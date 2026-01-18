from pathlib import Path

import typer

from gcal import GoogleCalendarClient, day_range


def main(
    credentials: Path = typer.Option(
        Path("credentials.json"),
        "--credentials",
    ),
    token: Path = typer.Option(
        Path("token.json"),
        "--token",
    ),
    max_results: int = typer.Option(
        50,
        "--max-results",
        min=1,
    ),
) -> None:
    client = GoogleCalendarClient(
        credentials_path=credentials,
        token_path=token,
    )
    start, end = day_range(-1, 1)
    events = client.list_events(start=start, end=end, max_results=max_results)

    if not events:
        print("No events found for today.")
        return

    for event in events:
        start_str = event.start.isoformat()
        print(f"{start_str}  {event.summary}")


if __name__ == "__main__":
    typer.run(main)
