from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

import daemon
from client import DaemonUnavailableError, SOCKET_PATH, send_request


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
    Trigger a reminder for the next un-acknowledged event.
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "test", "important": important})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    event_id = data.get("event_id")
    if event_id:
        print(f"Spawned reminder for {event_id}")


@app.command()
def show(
    event_id: str,
    important: bool = typer.Option(False, "--important"),
) -> None:
    """
    Show an event and wait for acknowledgement (used by spawned reminder terminals).
    """
    try:
        resp = send_request(SOCKET_PATH, {"cmd": "get_event", "id": event_id})
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)

    data = _require_ok(resp)
    e = data.get("event") or {}

    print(e.get("summary", "(no title)"))
    print(f"Start: {e.get('start', '?')}")
    print(f"End:   {e.get('end', '?')}")
    print(f"All-day: {bool(e.get('all_day', False))}")
    if e.get("html_link"):
        print(f"Link:  {e.get('html_link')}")
    print()

    try:
        if important:
            while True:
                ans = input("Type 'yes' to acknowledge: ").strip().lower()
                if ans == "yes":
                    break
        else:
            input("Press Enter to acknowledge...")
    except KeyboardInterrupt:
        # Do not ack if user exits early.
        raise typer.Exit(code=130)

    try:
        _require_ok(send_request(SOCKET_PATH, {"cmd": "ack", "id": event_id}))
    except DaemonUnavailableError as e:
        print(f"{e}. Start it with: uv run python main.py start")
        raise typer.Exit(code=1)
    print("Acknowledged.")


if __name__ == "__main__":
    app()
