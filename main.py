import argparse
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def get_local_tz():
    # Best-effort local timezone. On macOS this is typically correct.
    return datetime.now().astimezone().tzinfo


def load_credentials(
    *,
    credentials_path: Path,
    token_path: Path,
    no_browser: bool,
) -> Credentials:
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if creds and creds.valid:
        return creds

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Missing {credentials_path}. Place your OAuth client JSON at that path."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    if no_browser:
        creds = flow.run_console()
    else:
        creds = flow.run_local_server(port=0)

    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def list_todays_events(*, creds: Credentials, max_results: int = 50) -> list[dict]:
    service = build("calendar", "v3", credentials=creds)

    tz = get_local_tz()
    start_of_day = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=start_of_day.isoformat(),
            timeMax=end_of_day.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return events_result.get("items", [])


def format_event_line(event: dict) -> str:
    start = event.get("start", {})
    start_str = start.get("dateTime") or start.get("date") or "?"
    summary = event.get("summary") or "(no title)"
    return f"{start_str}  {summary}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect to Google Calendar and list today's events.")
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to Google OAuth client credentials JSON (Installed app). Default: credentials.json",
    )
    parser.add_argument(
        "--token",
        default="token.json",
        help="Path to token cache JSON. Default: token.json",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Use console auth flow instead of opening a browser.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Maximum number of events to print. Default: 50",
    )
    args = parser.parse_args()

    creds = load_credentials(
        credentials_path=Path(args.credentials),
        token_path=Path(args.token),
        no_browser=args.no_browser,
    )
    events = list_todays_events(creds=creds, max_results=args.max_results)

    if not events:
        print("No events found for today.")
        return 0

    for event in events:
        print(format_event_line(event))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
