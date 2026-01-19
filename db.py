from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, NamedTuple


ReminderKind = Literal["rule", "custom"]


class ReminderRef(NamedTuple):
    kind: ReminderKind
    id: int

    @staticmethod
    def parse(value: str) -> "ReminderRef":
        """
        Parse a reminder reference.

        We use a string form to avoid ID collisions between rule/custom tables:
        - "r:<id>" for rule reminders
        - "c:<id>" for custom reminders
        """
        s = value.strip()
        if s.startswith("r:"):
            return ReminderRef("rule", int(s[2:]))
        if s.startswith("c:"):
            return ReminderRef("custom", int(s[2:]))
        raise ValueError("invalid_reminder_id")

    def to_external_id(self) -> str:
        return ("r:" if self.kind == "rule" else "c:") + str(self.id)


@dataclass(frozen=True, slots=True)
class OccurrenceRow:
    event_id: str
    start_utc: int
    end_utc: int
    all_day: int
    dropped: int
    last_seen_utc: int


@dataclass(frozen=True, slots=True)
class ReminderRow:
    ref: ReminderRef
    event_id: str
    occ_start_utc: int
    trigger_utc: int
    requires_ack: int
    created_utc: int
    fired_utc: int | None
    acked_utc: int | None
    cancelled_utc: int | None
    rule_name: str | None = None  # only for rule reminders


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS occurrences (
          event_id TEXT NOT NULL,
          start_utc INTEGER NOT NULL,
          end_utc INTEGER NOT NULL,
          all_day INTEGER NOT NULL,
          dropped INTEGER NOT NULL DEFAULT 0,
          last_seen_utc INTEGER NOT NULL,
          PRIMARY KEY (event_id, start_utc)
        );

        CREATE TABLE IF NOT EXISTS rule_reminders (
          id INTEGER PRIMARY KEY,
          event_id TEXT NOT NULL,
          occ_start_utc INTEGER NOT NULL,
          rule_name TEXT NOT NULL,
          trigger_utc INTEGER NOT NULL,
          requires_ack INTEGER NOT NULL,
          created_utc INTEGER NOT NULL,
          acked_utc INTEGER NULL,
          fired_utc INTEGER NULL,
          cancelled_utc INTEGER NULL,
          FOREIGN KEY (event_id, occ_start_utc) REFERENCES occurrences(event_id, start_utc)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_reminders_occ_rule
          ON rule_reminders(event_id, occ_start_utc, rule_name);

        CREATE INDEX IF NOT EXISTS ix_rule_reminders_trigger
          ON rule_reminders(trigger_utc);

        CREATE TABLE IF NOT EXISTS custom_reminders (
          id INTEGER PRIMARY KEY,
          event_id TEXT NOT NULL,
          occ_start_utc INTEGER NOT NULL,
          trigger_utc INTEGER NOT NULL,
          requires_ack INTEGER NOT NULL,
          created_utc INTEGER NOT NULL,
          acked_utc INTEGER NULL,
          fired_utc INTEGER NULL,
          cancelled_utc INTEGER NULL,
          FOREIGN KEY (event_id, occ_start_utc) REFERENCES occurrences(event_id, start_utc)
        );

        CREATE INDEX IF NOT EXISTS ix_custom_reminders_trigger
          ON custom_reminders(trigger_utc);
        """
    )
    conn.commit()


def upsert_occurrence(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    start_utc: int,
    end_utc: int,
    all_day: int,
    last_seen_utc: int,
) -> tuple[bool, bool]:
    """
    Upsert an occurrence.

    Returns: (inserted, changed_core_fields)
    """
    row = conn.execute(
        """
        SELECT end_utc, all_day, dropped
        FROM occurrences
        WHERE event_id = ? AND start_utc = ?
        """,
        (event_id, start_utc),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO occurrences(event_id, start_utc, end_utc, all_day, dropped, last_seen_utc)
            VALUES(?,?,?,?,0,?)
            """,
            (event_id, start_utc, end_utc, all_day, last_seen_utc),
        )
        return True, True

    changed = (int(row["end_utc"]) != end_utc) or (int(row["all_day"]) != all_day)
    conn.execute(
        """
        UPDATE occurrences
        SET end_utc = ?, all_day = ?, last_seen_utc = ?
        WHERE event_id = ? AND start_utc = ?
        """,
        (end_utc, all_day, last_seen_utc, event_id, start_utc),
    )
    return False, changed


def get_occurrence(
    conn: sqlite3.Connection, *, event_id: str, start_utc: int
) -> OccurrenceRow | None:
    row = conn.execute(
        """
        SELECT event_id, start_utc, end_utc, all_day, dropped, last_seen_utc
        FROM occurrences
        WHERE event_id = ? AND start_utc = ?
        """,
        (event_id, start_utc),
    ).fetchone()
    if row is None:
        return None
    return OccurrenceRow(
        event_id=str(row["event_id"]),
        start_utc=int(row["start_utc"]),
        end_utc=int(row["end_utc"]),
        all_day=int(row["all_day"]),
        dropped=int(row["dropped"]),
        last_seen_utc=int(row["last_seen_utc"]),
    )


def list_occurrences_not_seen_since(
    conn: sqlite3.Connection, *, seen_cutoff_utc: int
) -> list[OccurrenceRow]:
    rows = conn.execute(
        """
        SELECT event_id, start_utc, end_utc, all_day, dropped, last_seen_utc
        FROM occurrences
        WHERE last_seen_utc < ?
        """,
        (seen_cutoff_utc,),
    ).fetchall()
    return [
        OccurrenceRow(
            event_id=str(r["event_id"]),
            start_utc=int(r["start_utc"]),
            end_utc=int(r["end_utc"]),
            all_day=int(r["all_day"]),
            dropped=int(r["dropped"]),
            last_seen_utc=int(r["last_seen_utc"]),
        )
        for r in rows
    ]


def ensure_rule_reminder(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    occ_start_utc: int,
    rule_name: str,
    trigger_utc: int,
    requires_ack: int,
    created_utc: int,
) -> bool:
    """
    Create a rule reminder if missing; if present and still pending, update trigger/ack requirement.

    Returns: True if inserted, False otherwise.
    """
    cur = conn.execute(
        """
        INSERT INTO rule_reminders(
          event_id, occ_start_utc, rule_name, trigger_utc, requires_ack, created_utc,
          acked_utc, fired_utc, cancelled_utc
        )
        VALUES(?,?,?,?,?,?,NULL,NULL,NULL)
        ON CONFLICT(event_id, occ_start_utc, rule_name) DO UPDATE SET
          trigger_utc = excluded.trigger_utc,
          requires_ack = excluded.requires_ack
        WHERE rule_reminders.acked_utc IS NULL
          AND rule_reminders.cancelled_utc IS NULL
        """,
        (event_id, occ_start_utc, rule_name, trigger_utc, requires_ack, created_utc),
    )
    return cur.rowcount == 1


def cancel_pending_rule_reminders_for_occurrence(
    conn: sqlite3.Connection, *, event_id: str, occ_start_utc: int, cancelled_utc: int
) -> int:
    cur = conn.execute(
        """
        UPDATE rule_reminders
        SET cancelled_utc = ?
        WHERE event_id = ? AND occ_start_utc = ?
          AND acked_utc IS NULL AND cancelled_utc IS NULL
        """,
        (cancelled_utc, event_id, occ_start_utc),
    )
    return int(cur.rowcount)


def insert_custom_reminder(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    occ_start_utc: int,
    trigger_utc: int,
    requires_ack: int,
    created_utc: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO custom_reminders(
          event_id, occ_start_utc, trigger_utc, requires_ack, created_utc,
          acked_utc, fired_utc, cancelled_utc
        )
        VALUES(?,?,?,?,?,NULL,NULL,NULL)
        """,
        (event_id, occ_start_utc, trigger_utc, requires_ack, created_utc),
    )
    return int(cur.lastrowid)


def cancel_pending_custom_reminders_for_occurrence(
    conn: sqlite3.Connection, *, event_id: str, occ_start_utc: int, cancelled_utc: int
) -> int:
    cur = conn.execute(
        """
        UPDATE custom_reminders
        SET cancelled_utc = ?
        WHERE event_id = ? AND occ_start_utc = ?
          AND acked_utc IS NULL AND cancelled_utc IS NULL
        """,
        (cancelled_utc, event_id, occ_start_utc),
    )
    return int(cur.rowcount)


def drop_occurrence(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    occ_start_utc: int,
    now_utc: int,
) -> tuple[int, int]:
    conn.execute(
        """
        UPDATE occurrences
        SET dropped = 1
        WHERE event_id = ? AND start_utc = ?
        """,
        (event_id, occ_start_utc),
    )
    n_rule = cancel_pending_rule_reminders_for_occurrence(
        conn, event_id=event_id, occ_start_utc=occ_start_utc, cancelled_utc=now_utc
    )
    n_custom = cancel_pending_custom_reminders_for_occurrence(
        conn, event_id=event_id, occ_start_utc=occ_start_utc, cancelled_utc=now_utc
    )
    return n_rule, n_custom


def _select_due_union(now_utc: int, limit: int) -> str:
    # Note: unify into a single ordered stream with a kind tag.
    return f"""
      SELECT
        'rule' AS kind,
        id,
        event_id,
        occ_start_utc,
        trigger_utc,
        requires_ack,
        created_utc,
        fired_utc,
        acked_utc,
        cancelled_utc,
        rule_name
      FROM rule_reminders
      WHERE trigger_utc <= ?
        AND fired_utc IS NULL
        AND acked_utc IS NULL
        AND cancelled_utc IS NULL

      UNION ALL

      SELECT
        'custom' AS kind,
        id,
        event_id,
        occ_start_utc,
        trigger_utc,
        requires_ack,
        created_utc,
        fired_utc,
        acked_utc,
        cancelled_utc,
        NULL AS rule_name
      FROM custom_reminders
      WHERE trigger_utc <= ?
        AND fired_utc IS NULL
        AND acked_utc IS NULL
        AND cancelled_utc IS NULL

      ORDER BY trigger_utc ASC
      LIMIT {int(limit)}
    """


def list_due_reminders(
    conn: sqlite3.Connection, *, now_utc: int, limit: int
) -> list[ReminderRow]:
    rows = conn.execute(_select_due_union(now_utc, limit), (now_utc, now_utc)).fetchall()
    out: list[ReminderRow] = []
    for r in rows:
        ref = ReminderRef(str(r["kind"]), int(r["id"]))  # type: ignore[arg-type]
        out.append(
            ReminderRow(
                ref=ref,
                event_id=str(r["event_id"]),
                occ_start_utc=int(r["occ_start_utc"]),
                trigger_utc=int(r["trigger_utc"]),
                requires_ack=int(r["requires_ack"]),
                created_utc=int(r["created_utc"]),
                fired_utc=(int(r["fired_utc"]) if r["fired_utc"] is not None else None),
                acked_utc=(int(r["acked_utc"]) if r["acked_utc"] is not None else None),
                cancelled_utc=(
                    int(r["cancelled_utc"]) if r["cancelled_utc"] is not None else None
                ),
                rule_name=(str(r["rule_name"]) if r["rule_name"] is not None else None),
            )
        )
    return out


def list_next_pending_reminders(
    conn: sqlite3.Connection, *, now_utc: int, limit: int
) -> list[ReminderRow]:
    rows = conn.execute(
        f"""
        SELECT
          'rule' AS kind,
          id,
          event_id,
          occ_start_utc,
          trigger_utc,
          requires_ack,
          created_utc,
          fired_utc,
          acked_utc,
          cancelled_utc,
          rule_name
        FROM rule_reminders
        WHERE trigger_utc > ?
          AND fired_utc IS NULL
          AND acked_utc IS NULL
          AND cancelled_utc IS NULL

        UNION ALL

        SELECT
          'custom' AS kind,
          id,
          event_id,
          occ_start_utc,
          trigger_utc,
          requires_ack,
          created_utc,
          fired_utc,
          acked_utc,
          cancelled_utc,
          NULL AS rule_name
        FROM custom_reminders
        WHERE trigger_utc > ?
          AND fired_utc IS NULL
          AND acked_utc IS NULL
          AND cancelled_utc IS NULL

        ORDER BY trigger_utc ASC
        LIMIT {int(limit)}
        """,
        (now_utc, now_utc),
    ).fetchall()

    out: list[ReminderRow] = []
    for r in rows:
        ref = ReminderRef(str(r["kind"]), int(r["id"]))  # type: ignore[arg-type]
        out.append(
            ReminderRow(
                ref=ref,
                event_id=str(r["event_id"]),
                occ_start_utc=int(r["occ_start_utc"]),
                trigger_utc=int(r["trigger_utc"]),
                requires_ack=int(r["requires_ack"]),
                created_utc=int(r["created_utc"]),
                fired_utc=(int(r["fired_utc"]) if r["fired_utc"] is not None else None),
                acked_utc=(int(r["acked_utc"]) if r["acked_utc"] is not None else None),
                cancelled_utc=(
                    int(r["cancelled_utc"]) if r["cancelled_utc"] is not None else None
                ),
                rule_name=(str(r["rule_name"]) if r["rule_name"] is not None else None),
            )
        )
    return out


def get_reminder(conn: sqlite3.Connection, ref: ReminderRef) -> ReminderRow | None:
    if ref.kind == "rule":
        row = conn.execute(
            """
            SELECT
              id, event_id, occ_start_utc, rule_name, trigger_utc, requires_ack, created_utc,
              fired_utc, acked_utc, cancelled_utc
            FROM rule_reminders
            WHERE id = ?
            """,
            (ref.id,),
        ).fetchone()
        if row is None:
            return None
        return ReminderRow(
            ref=ref,
            event_id=str(row["event_id"]),
            occ_start_utc=int(row["occ_start_utc"]),
            trigger_utc=int(row["trigger_utc"]),
            requires_ack=int(row["requires_ack"]),
            created_utc=int(row["created_utc"]),
            fired_utc=(int(row["fired_utc"]) if row["fired_utc"] is not None else None),
            acked_utc=(int(row["acked_utc"]) if row["acked_utc"] is not None else None),
            cancelled_utc=(
                int(row["cancelled_utc"]) if row["cancelled_utc"] is not None else None
            ),
            rule_name=str(row["rule_name"]),
        )

    row = conn.execute(
        """
        SELECT
          id, event_id, occ_start_utc, trigger_utc, requires_ack, created_utc,
          fired_utc, acked_utc, cancelled_utc
        FROM custom_reminders
        WHERE id = ?
        """,
        (ref.id,),
    ).fetchone()
    if row is None:
        return None
    return ReminderRow(
        ref=ref,
        event_id=str(row["event_id"]),
        occ_start_utc=int(row["occ_start_utc"]),
        trigger_utc=int(row["trigger_utc"]),
        requires_ack=int(row["requires_ack"]),
        created_utc=int(row["created_utc"]),
        fired_utc=(int(row["fired_utc"]) if row["fired_utc"] is not None else None),
        acked_utc=(int(row["acked_utc"]) if row["acked_utc"] is not None else None),
        cancelled_utc=(
            int(row["cancelled_utc"]) if row["cancelled_utc"] is not None else None
        ),
        rule_name=None,
    )


def mark_fired(conn: sqlite3.Connection, ref: ReminderRef, *, fired_utc: int) -> None:
    if ref.kind == "rule":
        conn.execute("UPDATE rule_reminders SET fired_utc = ? WHERE id = ?", (fired_utc, ref.id))
        return
    conn.execute(
        "UPDATE custom_reminders SET fired_utc = ? WHERE id = ?", (fired_utc, ref.id)
    )


def ack_reminder(conn: sqlite3.Connection, ref: ReminderRef, *, acked_utc: int) -> bool:
    if ref.kind == "rule":
        cur = conn.execute(
            """
            UPDATE rule_reminders
            SET acked_utc = ?
            WHERE id = ? AND acked_utc IS NULL
            """,
            (acked_utc, ref.id),
        )
        return cur.rowcount == 1
    cur = conn.execute(
        """
        UPDATE custom_reminders
        SET acked_utc = ?
        WHERE id = ? AND acked_utc IS NULL
        """,
        (acked_utc, ref.id),
    )
    return cur.rowcount == 1


def cancel_unseen_rule_reminders(
    conn: sqlite3.Connection, *, unseen_before_utc: int, cancelled_utc: int
) -> int:
    """
    Cancel pending rule reminders for occurrences that were not seen in the latest sync pass.
    """
    cur = conn.execute(
        """
        UPDATE rule_reminders
        SET cancelled_utc = ?
        WHERE cancelled_utc IS NULL
          AND acked_utc IS NULL
          AND fired_utc IS NULL
          AND EXISTS (
            SELECT 1
            FROM occurrences o
            WHERE o.event_id = rule_reminders.event_id
              AND o.start_utc = rule_reminders.occ_start_utc
              AND o.last_seen_utc < ?
          )
        """,
        (cancelled_utc, unseen_before_utc),
    )
    return int(cur.rowcount)

