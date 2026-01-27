from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

import daemon
from client import DaemonUnavailableError, SOCKET_PATH, send_request
from reminders import local_tz


app = typer.Typer(add_completion=False)


def _require_ok(resp: dict[str, Any]) -> dict[str, Any]:
    if resp.get("ok") is True:
        return resp
    err = resp.get("error") or "unknown_error"
    print(f"Error: {err}")
    raise typer.Exit(code=1)


@app.command()
def start(
    credentials: Path = typer.Option(Path("credentials.json"), "--credentials"),
    token: Path = typer.Option(Path("token.json"), "--token"),
) -> None:
    """
    Start the daemon in the foreground.
    """
    daemon.run(
        sock_path=SOCKET_PATH,
        credentials_path=credentials,
        token_path=token,
        project_root=Path(__file__).resolve().parent,
    )


@app.command("gcal-error")
def gcal_error(
    reason: str = typer.Option("", "--reason"),
) -> None:
    """
    Show a Google Calendar auth error (used by daemon notifications).
    """
    print("Google Calendar connection failed.")
    if reason:
        print()
        print(f"Reason: {reason}")
    print()
    print("How to fix:")
    print("1) Stop the LaunchAgent (or kill the daemon process).")
    print("2) Delete token.json in the project root.")
    print("3) Run once manually to re-authorize (opens browser):")
    print("   uv run python main.py start")
    print("4) Restart the LaunchAgent (./restart.sh).")
    print()
    try:
        input("Press Enter to close...")
    except KeyboardInterrupt:
        pass


@app.command()
def ping() -> None:
    """
    Check whether the daemon is reachable.
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "ping"})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    print(f"ok pid={data.get('pid')} now_utc={data.get('now_utc')}")


@app.command()
def due(n: int = typer.Argument(10, metavar="N")) -> None:
    """
    List due reminders.
    """
    if n < 1:
        raise typer.Exit(code=2)

    try:
        resp = send_request(SOCKET_PATH, {"cmd": "due", "limit": n})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    rs = data.get("reminders") or []
    if not rs:
        print("No due reminders.")
        return

    for r in rs:
        summary = r.get("summary") or r.get("event_id")
        print(
            f"{r.get('trigger_local','?')}  {r.get('id')}  "
            f"{r.get('rule_name') or 'custom'}  {summary}"
        )


@app.command()
def pending(n: int = typer.Argument(10, metavar="N")) -> None:
    """
    List next pending (future) reminders.
    """
    if n < 1:
        raise typer.Exit(code=2)

    try:
        resp = send_request(SOCKET_PATH, {"cmd": "pending", "limit": n})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    rs = data.get("reminders") or []
    if not rs:
        print("No pending reminders.")
        return

    for r in rs:
        summary = r.get("summary") or r.get("event_id")
        print(
            f"{r.get('trigger_local','?')}  {r.get('id')}  "
            f"{r.get('rule_name') or 'custom'}  {summary}"
        )


@app.command("regen-rules")
def regen_rules() -> None:
    """
    Regenerate rule-based reminders (best-effort).

    This restores future rule reminders that were previously marked fired (e.g. via older test behavior),
    and then runs a sync pass to ensure missing rule reminders exist.
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "regen_rules"})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    print(f"ok unfired={data.get('unfired')}")


@app.command("next")
def next_events(n: int = typer.Argument(..., metavar="N")) -> None:
    """
    Print the next N events within the next 30 days.
    """
    if n < 1:
        raise typer.Exit(code=2)

    try:
        resp = send_request(SOCKET_PATH, {"cmd": "next", "limit": n})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    events = data.get("events") or []
    if not events:
        print("No upcoming events.")
        return

    for e in events:
        start = e.get("start", "?")
        summary = e.get("summary", "(no title)")
        event_id = e.get("id", "")
        print(f"{start}  {summary}  {event_id}")


@app.command()
def test(important: bool = typer.Option(False, "--important")) -> None:
    """
    Trigger a reminder (debug helper).
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "test", "important": important})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    rid = data.get("reminder_id")
    if rid:
        print(f"Spawned reminder {rid}")


def _parse_local_datetime_to_trigger_utc(value: str) -> int:
    """
    Parse a local datetime input and return UTC epoch seconds.

    Accepted:
    - ISO: YYYY-MM-DDTHH:MM[:SS]
    - Space-separated: YYYY-MM-DD HH:MM[:SS]
    """
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise typer.BadParameter("Invalid datetime. Use YYYY-MM-DD HH:MM (local) or ISO.") from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return int(dt.astimezone(timezone.utc).timestamp())


@app.command("show-reminder")
def show_reminder(
    reminder_id: str,
    important: bool = typer.Option(False, "--important"),
) -> None:
    """
    Show a reminder and prompt for ack/drop/snooze (used by spawned reminder terminals).
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "get_reminder", "id": reminder_id})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    r = data.get("reminder") or {}
    e = data.get("event") or {}

    print(e.get("summary", "(no title)"))
    print(f"Start: {e.get('start', '?')}")
    print(f"End:   {e.get('end', '?')}")
    print(f"All-day: {bool(e.get('all_day', False))}")
    print(f"Reminder: {r.get('id', reminder_id)}")
    if r.get("rule_name"):
        print(f"Rule: {r.get('rule_name')}")
    if r.get("trigger_local"):
        print(f"Trigger: {r.get('trigger_local')}")
    if e.get("html_link"):
        print(f"Link:  {e.get('html_link')}")
    print()

    requires_ack = bool(r.get("requires_ack", False))
    prompt_hard = important or requires_ack

    try:
        if prompt_hard:
            while True:
                ans = input("Enter command (ack/drop/snooze <YYYY-MM-DD HH:MM>): ").strip()
                if ans:
                    break
        else:
            ans = input("Enter (ack/drop/snooze <YYYY-MM-DD HH:MM>, blank=ack): ").strip()
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    ans_l = ans.strip().lower()

    if ans_l == "" or ans_l == "ack":
        try:
            _require_ok(send_request(SOCKET_PATH, {"cmd": "ack_reminder", "id": reminder_id}))
        except DaemonUnavailableError as e:
            print(f"{e}. Start it with: uv run python main.py start")
            raise typer.Exit(code=1)
        print("Acknowledged.")
        return

    if ans_l == "drop":
        event_id = r.get("event_id")
        occ_start_utc = r.get("occ_start_utc")
        if not isinstance(event_id, str) or not isinstance(occ_start_utc, int):
            print("Error: reminder missing event_id/occ_start_utc")
            raise typer.Exit(code=1)

        try:
            _require_ok(
                send_request(
                    SOCKET_PATH,
                    {"cmd": "drop_occurrence", "event_id": event_id, "occ_start_utc": occ_start_utc},
                )
            )
        except DaemonUnavailableError as e:
            print(f"{e}. Start it with: uv run python main.py start")
            raise typer.Exit(code=1)
        print("Dropped.")
        return

    if ans_l.startswith("snooze "):
        when = ans.strip()[7:].strip()
        if not when:
            print("Error: snooze requires a datetime, e.g. snooze 2026-01-19 17:00")
            raise typer.Exit(code=2)
        trigger_utc = _parse_local_datetime_to_trigger_utc(when)

        try:
            resp2 = send_request(
                SOCKET_PATH,
                {"cmd": "snooze", "id": reminder_id, "trigger_utc": trigger_utc},
            )
        except DaemonUnavailableError as e:
            print(f"{e}. Start it with: uv run python main.py start")
            raise typer.Exit(code=1)

        data2 = _require_ok(resp2)
        new_id = data2.get("new_id")
        if new_id:
            print(f"Snoozed -> {new_id}")
        else:
            print("Snoozed.")
        return

    print("Unknown command. Use: ack | drop | snooze <YYYY-MM-DD HH:MM>")
    raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
