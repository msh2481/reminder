from __future__ import annotations

import json
import socket
from typing import Any

SOCKET_PATH = "/tmp/remind.sock"


class DaemonUnavailableError(RuntimeError):
    pass


def send_request(sock_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send one JSON request and receive one JSON response.

    Protocol:
    - client sends a single line JSON (newline-delimited)
    - server responds with a single line JSON and closes
    """
    msg = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.sendall(msg.encode("utf-8"))

            f = s.makefile("r", encoding="utf-8", newline="\n")
            line = f.readline()
    except FileNotFoundError as e:
        raise DaemonUnavailableError(f"Daemon socket not found at {sock_path}") from e
    except ConnectionRefusedError as e:
        raise DaemonUnavailableError(f"Daemon not accepting connections at {sock_path}") from e

    if not line:
        raise RuntimeError("No response from daemon")

    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response from daemon: {line!r}") from e

    if not isinstance(obj, dict):
        raise RuntimeError(f"Invalid response type from daemon: {type(obj)}")

    return obj

