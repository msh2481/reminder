from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone


def local_tz():
    return datetime.now().astimezone().tzinfo


def to_utc_epoch_seconds(dt: datetime) -> int:
    """
    Convert a datetime to UTC epoch seconds (int).
    If dt is naive, interpret it as local time.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return int(dt.astimezone(timezone.utc).timestamp())


@dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_name: str
    trigger_local: datetime
    requires_ack: int  # 0/1


def _six_am_on_event_date(event_start_local: datetime) -> datetime:
    tz = event_start_local.tzinfo or local_tz()
    d = event_start_local.date()
    return datetime.combine(d, time(6, 0), tz)


def compute_rule_specs(event_start_local: datetime, event_end_local: datetime, all_day: bool) -> list[RuleSpec]:
    """
    Compute rule reminders from an event occurrence.

    All computations are in local time; conversion to UTC happens when persisting.
    """
    if event_start_local.tzinfo is None:
        event_start_local = event_start_local.replace(tzinfo=local_tz())
    if event_end_local.tzinfo is None:
        event_end_local = event_end_local.replace(tzinfo=local_tz())

    return [
        RuleSpec(
            rule_name="week_before",
            trigger_local=event_start_local - timedelta(days=7),
            requires_ack=0,
        ),
        RuleSpec(
            rule_name="day_before",
            trigger_local=event_start_local - timedelta(days=1),
            requires_ack=1,
        ),
        RuleSpec(
            rule_name="six_am",
            trigger_local=_six_am_on_event_date(event_start_local),
            requires_ack=0,
        ),
        RuleSpec(
            rule_name="minus_30m",
            trigger_local=event_start_local - timedelta(minutes=30),
            requires_ack=1,
        ),
    ]

