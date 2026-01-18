from __future__ import annotations

import json
import shlex
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from client import SOCKET_PATH
from gcal import Event, GoogleCalendarClient, day_range
from utils import play_sound, spawn_terminal

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
    acked_ids: set[str] = field(default_factory=set)
    event_cache: dict[str, Event] = field(default_factory=dict)

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

    def choose_next_unacked_event(self) -> Event | None:
        events = self.refresh_events()
        for e in events:
            if not e.id:
                continue
            if e.id in self.acked_ids:
                continue
            return e
        return None

    def spawn_reminder(self, event_id: str, *, important: bool) -> None:
        # Play sound best-effort; we still want to spawn the terminal even if sound fails.
        sound_path = self.project_root / "beep.wav"
        play_sound(sound_path)

        # Spawn a new iTerm2 window that runs the show flow via uv.
        show_cmd = (
            f"cd {shlex.quote(str(self.project_root))}"
            f" && uv run python main.py show {shlex.quote(event_id)}"
        )
        if important:
            show_cmd += " --important"

        # Close the spawned session after acknowledgement.
        show_cmd += "; exit"

        spawn_terminal(show_cmd)


def _handle_request(state: DaemonState, req: dict[str, Any]) -> dict[str, Any]:
    cmd = req.get("cmd")
    if cmd == "next":
        limit = int(req.get("limit", 5))
        if limit < 1:
            return {"ok": False, "error": "limit_must_be_positive"}

        events = state.refresh_events()
        # Keep only events with ids so client can reference them.
        out = [_event_to_dict(e) for e in events if e.id][:limit]
        return {"ok": True, "events": out}

    if cmd == "get_event":
        event_id = req.get("id")
        if not isinstance(event_id, str) or not event_id:
            return {"ok": False, "error": "missing_id"}

        e = state.get_event(event_id)
        if e is None:
            return {"ok": False, "error": "event_not_found"}
        return {"ok": True, "event": _event_to_dict(e)}

    if cmd == "ack":
        event_id = req.get("id")
        if not isinstance(event_id, str) or not event_id:
            return {"ok": False, "error": "missing_id"}

        state.acked_ids.add(event_id)
        return {"ok": True}

    if cmd == "test":
        important = bool(req.get("important", False))
        e = state.choose_next_unacked_event()
        if e is None or not e.id:
            return {"ok": False, "error": "no_upcoming_events"}

        state.spawn_reminder(e.id, important=important)
        return {"ok": True, "event_id": e.id}

    return {"ok": False, "error": "unknown_cmd"}


def run(
    *,
    sock_path: str = SOCKET_PATH,
    credentials_path: Path = Path("credentials.json"),
    token_path: Path = Path("token.json"),
    project_root: Path | None = None,
) -> None:
    project_root = project_root or Path(__file__).resolve().parent
    state = DaemonState(
        gcal=GoogleCalendarClient(credentials_path=credentials_path, token_path=token_path),
        project_root=project_root,
    )

    sock_file = Path(sock_path)
    if sock_file.exists():
        sock_file.unlink()

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(sock_path)
        server.listen(50)

        try:
            while True:
                conn, _ = server.accept()
                with conn:
                    f_r = conn.makefile("r", encoding="utf-8", newline="\n")
                    f_w = conn.makefile("w", encoding="utf-8", newline="\n")
                    line = f_r.readline()
                    if not line:
                        continue

                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        f_w.write(json.dumps({"ok": False, "error": "invalid_json"}) + "\n")
                        f_w.flush()
                        continue

                    if not isinstance(req, dict):
                        f_w.write(json.dumps({"ok": False, "error": "invalid_request"}) + "\n")
                        f_w.flush()
                        continue

                    resp = _handle_request(state, req)
                    f_w.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    f_w.flush()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                sock_file.unlink()
            except FileNotFoundError:
                pass

