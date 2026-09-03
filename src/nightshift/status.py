"""Live run status for the CLI and the command deck."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HALT_REQUEST = "halt.request"


@dataclass
class RunStatus:
    state: str = "idle"
    runner_pid: int | None = None
    repo: str = ""
    branch: str = ""
    brain: str = ""
    remaining_count: int | None = None
    turn: int = 0
    last_check: dict[str, Any] = field(default_factory=dict)
    halt_reason: str = ""
    summary: str = ""
    error: str = ""
    loopscope_url: str = "http://127.0.0.1:7788"
    mock: bool = False
    updated_at: str = ""
    refused: list[str] = field(default_factory=list)
    brief: dict[str, Any] = field(default_factory=dict)
    job_upgrade_id: int = 0
    job: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    fail_streak: dict[str, Any] = field(default_factory=dict)
    turns: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    deadline: float = 0.0
    max_turns: int = 0
    host_python: str = ""
    main_untouched: bool = True
    halt_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StatusBoard:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "status.json"
        self._lock = threading.Lock()
        self.current = RunStatus()

    def update(self, **kwargs: Any) -> RunStatus:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self.current, key):
                    setattr(self.current, key, value)
            self.current.updated_at = datetime.now(timezone.utc).isoformat()
            self.path.write_text(
                json.dumps(self.current.to_dict(), indent=2), encoding="utf-8"
            )
            return self.current

    def read(self) -> RunStatus:
        with self._lock:
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    self.current = RunStatus(
                        **{
                            k: v
                            for k, v in data.items()
                            if k in RunStatus.__dataclass_fields__
                        }
                    )
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
            return self.current

    def snapshot(self) -> dict[str, Any]:
        return self.read().to_dict()


def request_halt(home: Path, pid: int) -> Path:
    path = Path(home) / HALT_REQUEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": int(pid),
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


def clear_halt(home: Path) -> None:
    path = Path(home) / HALT_REQUEST
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def halt_requested(home: Path, pid: int) -> bool:
    path = Path(home) / HALT_REQUEST
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stored = int(data.get("pid") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        try:
            path.unlink()
        except OSError:
            pass
        return False
    if stored != int(pid):
        try:
            path.unlink()
        except OSError:
            pass
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def live_owner(runner_pid: int | None, *, self_pid: int | None = None) -> bool:
    """True if runner_pid looks like a live Nightshift process (not this deck)."""
    if runner_pid is None:
        return False
    me = os.getpid() if self_pid is None else self_pid
    if runner_pid == me:
        return False
    try:
        os.kill(runner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True
