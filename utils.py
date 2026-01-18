from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _escape_applescript_string(value: str) -> str:
    # AppleScript string literal uses double quotes; escape backslashes and quotes.
    return value.replace("\\", "\\\\").replace('"', '\\"')


def spawn_terminal(command: str) -> None:
    """
    Spawn a new terminal window running `command`.

    macOS MVP implementation targets iTerm2 via `osascript`.
    """
    if sys.platform != "darwin":
        raise NotImplementedError(f"spawn_terminal is not implemented for {sys.platform!r}")

    cmd_escaped = _escape_applescript_string(command)
    script = f"""
tell application "iTerm2"
  create window with default profile
  tell current session of current window
    write text "{cmd_escaped}"
  end tell
end tell
""".strip()

    subprocess.run(["osascript", "-e", script], check=True)


def play_sound(path: str | Path) -> None:
    """
    Play a sound file.

    macOS MVP implementation uses `afplay` (best-effort).
    """
    if sys.platform != "darwin":
        raise NotImplementedError(f"play_sound is not implemented for {sys.platform!r}")

    p = Path(path)
    subprocess.run(["afplay", str(p)], check=False)

