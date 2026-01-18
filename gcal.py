from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

DEFAULT_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_rfc3339_datetime(value: str) -> datetime:
    # Google sometimes returns `Z` suffix for UTC.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _as_tzaware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_local_tz())
    return dt


@dataclass(frozen=True, slots=True)
class Event:
    id: str | None
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    html_link: str | None = None

    @staticmethod
    def from_api(event: dict) -> "Event":
        tz = _local_tz()

        start_obj = event.get("start") or {}
        end_obj = event.get("end") or {}

        start_dt_str = start_obj.get("dateTime")
        end_dt_str = end_obj.get("dateTime")
        start_date_str = start_obj.get("date")
        end_date_str = end_obj.get("date")

        if start_dt_str and end_dt_str:
            start = _parse_rfc3339_datetime(start_dt_str)
            end = _parse_rfc3339_datetime(end_dt_str)
            all_day = False
        elif start_date_str and end_date_str:
            # All-day events are date-only; end is typically next day.
            start_date = date.fromisoformat(start_date_str)
            end_date = date.fromisoformat(end_date_str)
            start = datetime.combine(start_date, time.min, tz)
            end = datetime.combine(end_date, time.min, tz)
            all_day = True
        else:
            # Fallback: treat missing times as unknown, but keep the object valid.
            now = datetime.now(tz)
            start = now
            end = now
            all_day = False

        return Event(
            id=event.get("id"),
            summary=event.get("summary") or "(no title)",
            start=start,
            end=end,
            all_day=all_day,
            html_link=event.get("htmlLink"),
        )


class GoogleCalendarClient:
    def __init__(
        self,
        *,
        credentials_path: str | Path = "credentials.json",
        token_path: str | Path = "token.json",
        scopes: list[str] | None = None,
    ) -> None:
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        self._scopes = scopes or DEFAULT_SCOPES

        self._creds = self._load_credentials()
        self._service = build("calendar", "v3", credentials=self._creds)

    def _load_credentials(self) -> Credentials:
        creds: Credentials | None = None

        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self._token_path), self._scopes
            )

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

        if creds and creds.valid:
            return creds

        if not self._credentials_path.exists():
            raise FileNotFoundError(
                f"Missing {self._credentials_path}. Place your OAuth client JSON at that path."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self._credentials_path), self._scopes
        )
        creds = flow.run_local_server(port=0)

        self._token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def list_events(
        self,
        start: datetime,
        end: datetime,
        max_results: int = 50,
        *,
        calendar_id: str = "primary",
    ) -> list[Event]:
        start = _as_tzaware(start)
        end = _as_tzaware(end)

        events_result = (
            self._service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = events_result.get("items", [])
        return [Event.from_api(item) for item in items]


def day_range(from_days: int, to_days: int) -> tuple[datetime, datetime]:
    """
    Return a (start, end) datetime range aligned to local midnights.

    Examples:
    - day_range(0, 1): interval corresponding to next 24h
    - day_range(-2, 2): interval from T-48h to T+48h
    """
    if to_days < from_days:
        raise ValueError("to_days must be >= from_days")

    tz = _local_tz()
    T = datetime.now(tz)
    start = T + timedelta(days=from_days)
    end = T + timedelta(days=to_days)
    return start, end

