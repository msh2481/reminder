from __future__ import annotations

import json
import os
import shlex
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from client import SOCKET_PATH
from db import (
    ReminderRef,
    ack_reminder,
    cancel_unseen_rule_reminders,
    connect,
    drop_occurrence,
    ensure_rule_reminder,
    get_occurrence,
    get_reminder,
    insert_custom_reminder,
    list_due_reminders,
    list_next_pending_reminders,
    mark_fired,
    migrate,
)
from gcal import Event, GoogleCalendarClient, day_range
from log import configure_logger
from loguru import logger
from reminders import compute_rule_specs, local_tz, to_utc_epoch_seconds
from utils import play_sound, spawn_terminal


def _now_utc() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _utc_to_local_iso(ts_utc: int) -> str:
    return datetime.fromtimestamp(ts_utc, tz=timezone.utc).astimezone(local_tz()).isoformat()


def _event_to_dict(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "summary": e.summary,
        "start": e.start.isoformat(),
        "end": e.end.isoformat(),
        "all_day": e.all_day,
        "html_link": e.html_link,
    }


@dataclass
class DaemonState:
    gcal: GoogleCalendarClient
    project_root: Path
    db_path: Path
    sync_interval_s: int = 60
    spawn_throttle_s: int = 10
    last_sync_utc: int = 0
    last_spawn_utc: int = 0
    event_cache: dict[str, Event] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.conn = connect(self.db_path)
        migrate(self.conn)

    def refresh_events(self) -> list[Event]:
        now, end = day_range(0, 30)
        # Pull more than we typically need so "next N" and "test" work reliably.
        events = self.gcal.list_events(start=now, end=end, max_results=2500)

        self.event_cache = {e.id: e for e in events if e.id}
        # Normalize ordering for downstream selection.
        events.sort(key=lambda e: e.start)
        return events

    def get_event(self, event_id: str) -> Event | None:
        e = self.event_cache.get(event_id)
        if e is not None:
            return e
        # Cache miss: refresh once.
        self.refresh_events()
        return self.event_cache.get(event_id)

    def sync(self) -> dict[str, int]:
        """
        Sync events from GCal into occurrences, ensure rule reminders exist,
        and cancel rule reminders for occurrences not seen in this pass.
        """
        started_utc = _now_utc()
        t0 = time.time()

        now_local, end_local = day_range(0, 30)
        events = self.gcal.list_events(start=now_local, end=end_local, max_results=2500)
        self.event_cache = {e.id: e for e in events if e.id}

        seen = 0
        occ_upserted = 0
        occ_changed = 0
        rule_changed = 0
        skipped_past = 0

        for e in events:
            if not e.id:
                continue
            seen += 1

            start_utc = to_utc_epoch_seconds(e.start)
            end_utc = to_utc_epoch_seconds(e.end)
            all_day = 1 if e.all_day else 0

            inserted, changed = self._upsert_occurrence(
                event_id=e.id,
                summary=e.summary,
                start_utc=start_utc,
                end_utc=end_utc,
                all_day=all_day,
                last_seen_utc=started_utc,
            )
            if inserted or changed:
                occ_upserted += 1
            if changed:
                occ_changed += 1

            occ = get_occurrence(self.conn, event_id=e.id, start_utc=start_utc)
            if occ is None or occ.dropped:
                continue

            specs = compute_rule_specs(e.start, e.end, e.all_day)
            for spec in specs:
                trig_utc = to_utc_epoch_seconds(spec.trigger_local)
                # Never create/update reminders whose computed trigger is already in the past.
                if trig_utc <= started_utc:
                    skipped_past += 1
                    continue
                changed_rule = ensure_rule_reminder(
                    self.conn,
                    event_id=e.id,
                    occ_start_utc=start_utc,
                    rule_name=spec.rule_name,
                    trigger_utc=trig_utc,
                    requires_ack=spec.requires_ack,
                    created_utc=started_utc,
                )
                if changed_rule:
                    rule_changed += 1

        cancelled_unseen = cancel_unseen_rule_reminders(
            self.conn, unseen_before_utc=started_utc, cancelled_utc=started_utc
        )

        self.conn.commit()
        self.last_sync_utc = started_utc

        logger.info(
            "sync pass complete seen_from_gcal={} occ_upserted={} occ_changed={} rule_changed={} "
            "rule_cancelled_unseen={} skipped_past={} duration_s={:.3f}",
            seen,
            occ_upserted,
            occ_changed,
            rule_changed,
            cancelled_unseen,
            skipped_past,
            time.time() - t0,
        )
        return {
            "seen_from_gcal": seen,
            "occ_upserted": occ_upserted,
            "occ_changed": occ_changed,
            "rule_changed": rule_changed,
            "rule_cancelled_unseen": cancelled_unseen,
            "skipped_past": skipped_past,
        }

    def _upsert_occurrence(
        self,
        *,
        event_id: str,
        summary: str,
        start_utc: int,
        end_utc: int,
        all_day: int,
        last_seen_utc: int,
    ) -> tuple[bool, bool]:
        from db import upsert_occurrence

        inserted, changed = upsert_occurrence(
            self.conn,
            event_id=event_id,
            start_utc=start_utc,
            end_utc=end_utc,
            all_day=all_day,
            last_seen_utc=last_seen_utc,
        )
        if inserted:
            logger.info(
                "occurrence added event_id={} start_local={} summary={!r}",
                event_id,
                _utc_to_local_iso(start_utc),
                summary,
            )
        elif changed:
            logger.info(
                "occurrence updated event_id={} start_local={} summary={!r}",
                event_id,
                _utc_to_local_iso(start_utc),
                summary,
            )
        return inserted, changed

    def spawn_reminder(self, reminder_id: str, *, important: bool) -> None:
        # Play sound best-effort; we still want to spawn the terminal even if sound fails.
        sound_path = self.project_root / "beep.wav"
        play_sound(sound_path)

        # Spawn a new iTerm2 window that runs the show flow via uv.
        show_cmd = (
            f"cd {shlex.quote(str(self.project_root))}"
            f" && uv run python main.py show-reminder {shlex.quote(reminder_id)}"
        )
        if important:
            show_cmd += " --important"

        # Close the spawned session after acknowledgement.
        show_cmd += "; exit"

        spawn_terminal(show_cmd)

    def fire_one_due(self, *, important: bool, ignore_throttle: bool) -> str | None:
        now_utc = _now_utc()
        if (not ignore_throttle) and self.last_spawn_utc and (now_utc - self.last_spawn_utc < self.spawn_throttle_s):
            return None

        due = list_due_reminders(self.conn, now_utc=now_utc, limit=1)
        if not due:
            return None

        r = due[0]
        mark_fired(self.conn, r.ref, fired_utc=now_utc)
        self.conn.commit()

        rid = r.ref.to_external_id()
        logger.info(
            "reminder fired id={} trigger_local={} event_id={} occ_start_local={}",
            rid,
            _utc_to_local_iso(r.trigger_utc),
            r.event_id,
            _utc_to_local_iso(r.occ_start_utc),
        )
        self.spawn_reminder(rid, important=important)
        self.last_spawn_utc = now_utc
        return rid

    def fire_one_any(self, *, important: bool) -> str | None:
        """
        Fire a reminder for debugging: prefer due; otherwise fire the next pending.
        """
        rid = self.fire_one_due(important=important, ignore_throttle=True)
        if rid is not None:
            return rid

        now_utc = _now_utc()
        nxt = list_next_pending_reminders(self.conn, now_utc=now_utc, limit=1)
        if not nxt:
            return None
        r = nxt[0]
        mark_fired(self.conn, r.ref, fired_utc=now_utc)
        self.conn.commit()

        rid = r.ref.to_external_id()
        logger.info("reminder test-fired id={} event_id={}", rid, r.event_id)
        self.spawn_reminder(rid, important=important)
        self.last_spawn_utc = now_utc
        return rid


def _handle_request(state: DaemonState, req: dict[str, Any]) -> dict[str, Any]:
    cmd = req.get("cmd")
    if cmd == "ping":
        return {"ok": True, "now_utc": _now_utc(), "pid": os.getpid()}

    if cmd == "sync":
        state.sync()
        return {"ok": True}

    if cmd == "next":
        limit = int(req.get("limit", 5))
        if limit < 1:
            return {"ok": False, "error": "limit_must_be_positive"}

        events = state.refresh_events()
        # Keep only events with ids so client can reference them.
        out = [_event_to_dict(e) for e in events if e.id][:limit]
        return {"ok": True, "events": out}

    if cmd == "due":
        limit = int(req.get("limit", 10))
        if limit < 1:
            return {"ok": False, "error": "limit_must_be_positive"}
        now_utc = _now_utc()
        due = list_due_reminders(state.conn, now_utc=now_utc, limit=limit)
        return {
            "ok": True,
            "reminders": [
                {
                    "id": r.ref.to_external_id(),
                    "kind": r.ref.kind,
                    "event_id": r.event_id,
                    "occ_start_utc": r.occ_start_utc,
                    "trigger_utc": r.trigger_utc,
                    "requires_ack": bool(r.requires_ack),
                    "rule_name": r.rule_name,
                }
                for r in due
            ],
        }

    if cmd == "fire_next":
        rid = state.fire_one_due(important=False, ignore_throttle=True)
        if rid is None:
            return {"ok": False, "error": "no_due_reminders"}
        return {"ok": True, "reminder_id": rid}

    if cmd == "get_reminder":
        rid = req.get("id")
        if not isinstance(rid, str) or not rid:
            return {"ok": False, "error": "missing_id"}
        try:
            ref = ReminderRef.parse(rid)
        except Exception:
            return {"ok": False, "error": "invalid_reminder_id"}

        r = get_reminder(state.conn, ref)
        if r is None:
            return {"ok": False, "error": "reminder_not_found"}

        occ = get_occurrence(state.conn, event_id=r.event_id, start_utc=r.occ_start_utc)
        if occ is None:
            return {"ok": False, "error": "occurrence_not_found"}

        ev = state.get_event(r.event_id)
        summary = ev.summary if ev is not None else "(event not found)"
        html_link = ev.html_link if ev is not None else None

        event_payload = {
            "id": r.event_id,
            "summary": summary,
            "start": _utc_to_local_iso(occ.start_utc),
            "end": _utc_to_local_iso(occ.end_utc),
            "all_day": bool(occ.all_day),
            "html_link": html_link,
        }
        reminder_payload = {
            "id": r.ref.to_external_id(),
            "kind": r.ref.kind,
            "rule_name": r.rule_name,
            "event_id": r.event_id,
            "occ_start_utc": r.occ_start_utc,
            "trigger_utc": r.trigger_utc,
            "trigger_local": _utc_to_local_iso(r.trigger_utc),
            "requires_ack": bool(r.requires_ack),
            "fired_utc": r.fired_utc,
            "acked_utc": r.acked_utc,
            "cancelled_utc": r.cancelled_utc,
        }
        return {"ok": True, "reminder": reminder_payload, "event": event_payload}

    if cmd == "ack_reminder":
        rid = req.get("id")
        if not isinstance(rid, str) or not rid:
            return {"ok": False, "error": "missing_id"}
        try:
            ref = ReminderRef.parse(rid)
        except Exception:
            return {"ok": False, "error": "invalid_reminder_id"}

        now_utc = _now_utc()
        changed = ack_reminder(state.conn, ref, acked_utc=now_utc)
        state.conn.commit()
        logger.info("reminder acked id={} changed={}", rid, changed)
        return {"ok": True, "changed": bool(changed)}

    if cmd == "drop_occurrence":
        event_id = req.get("event_id")
        occ_start_utc = req.get("occ_start_utc")
        if not isinstance(event_id, str) or not event_id:
            return {"ok": False, "error": "missing_event_id"}
        if not isinstance(occ_start_utc, int):
            return {"ok": False, "error": "missing_occ_start_utc"}

        now_utc = _now_utc()
        n_rule, n_custom = drop_occurrence(
            state.conn, event_id=event_id, occ_start_utc=occ_start_utc, now_utc=now_utc
        )
        state.conn.commit()
        logger.info(
            "occurrence dropped event_id={} occ_start_local={} cancelled_rule={} cancelled_custom={}",
            event_id,
            _utc_to_local_iso(occ_start_utc),
            n_rule,
            n_custom,
        )
        return {"ok": True, "cancelled_rule": n_rule, "cancelled_custom": n_custom}

    if cmd == "snooze":
        rid = req.get("id")
        trigger_utc = req.get("trigger_utc")
        if not isinstance(rid, str) or not rid:
            return {"ok": False, "error": "missing_id"}
        if not isinstance(trigger_utc, int):
            return {"ok": False, "error": "missing_trigger_utc"}
        now_utc = _now_utc()
        if trigger_utc <= now_utc:
            return {"ok": False, "error": "trigger_in_past"}

        try:
            ref = ReminderRef.parse(rid)
        except Exception:
            return {"ok": False, "error": "invalid_reminder_id"}
        src = get_reminder(state.conn, ref)
        if src is None:
            return {"ok": False, "error": "reminder_not_found"}

        new_id = insert_custom_reminder(
            state.conn,
            event_id=src.event_id,
            occ_start_utc=src.occ_start_utc,
            trigger_utc=trigger_utc,
            requires_ack=1,
            created_utc=now_utc,
        )
        ack_reminder(state.conn, ref, acked_utc=now_utc)
        state.conn.commit()

        new_rid = ReminderRef("custom", new_id).to_external_id()
        logger.info(
            "reminder snoozed source_id={} new_id={} new_trigger_local={}",
            rid,
            new_rid,
            _utc_to_local_iso(trigger_utc),
        )
        return {"ok": True, "new_id": new_rid}

    if cmd == "test":
        important = bool(req.get("important", False))
        # Ensure we have some data to test with.
        if state.last_sync_utc == 0:
            state.sync()
        rid = state.fire_one_any(important=important)
        if rid is None:
            return {"ok": False, "error": "no_reminders"}
        return {"ok": True, "reminder_id": rid}

    return {"ok": False, "error": "unknown_cmd"}


def run(
    *,
    sock_path: str = SOCKET_PATH,
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
    project_root: Path | None = None,
) -> None:
    project_root = project_root or Path(__file__).resolve().parent
    configure_logger(project_root)
    logger.info(
        "daemon startup project_root={} sock_path={} db_path={}",
        str(project_root),
        sock_path,
        str(project_root / "reminder.db"),
    )

    state = DaemonState(
        gcal=GoogleCalendarClient(credentials_path=credentials_path, token_path=token_path),
        project_root=project_root,
        db_path=project_root / "reminder.db",
    )
    # Initial sync so scheduler has data.
    state.sync()

    sock_file = Path(sock_path)
    if sock_file.exists():
        sock_file.unlink()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(sock_path)
        server.listen(50)
        server.settimeout(1.0)

        try:
            while True:
                # Scheduler tick
                now_utc = _now_utc()
                if now_utc - state.last_sync_utc >= state.sync_interval_s:
                    state.sync()
                # Fire overdue reminders (catch-up behavior) + due reminders.
                state.fire_one_due(important=False, ignore_throttle=False)

                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue

                with conn:
                    f_r = conn.makefile("r", encoding="utf-8", newline="\n")
                    f_w = conn.makefile("w", encoding="utf-8", newline="\n")
                    line = f_r.readline()
                    if not line:
                        continue

                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        resp = {"ok": False, "error": "invalid_json"}
                        f_w.write(json.dumps(resp) + "\n")
                        f_w.flush()
                        continue

                    if not isinstance(req, dict):
                        resp = {"ok": False, "error": "invalid_request"}
                        f_w.write(json.dumps(resp) + "\n")
                        f_w.flush()
                        continue

                    cmd = req.get("cmd")
                    logger.info("socket request cmd={} req={}", cmd, req)
                    resp = _handle_request(state, req)
                    logger.info("socket response cmd={} resp={}", cmd, resp)
                    f_w.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    f_w.flush()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                sock_file.unlink()
            except FileNotFoundError:
                pass
            try:
                state.conn.close()
            except Exception:
                pass
            logger.info("daemon shutdown")

