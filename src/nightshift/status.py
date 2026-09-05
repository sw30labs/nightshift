"""Live run status for the CLI and the command deck."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .forum import atomic_write_json, with_home_lock

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
        """Merge into the latest on-disk board, including updates from other writers."""
        with self._lock:
            return with_home_lock(self.home, "status", lambda: self._save(kwargs))

    def reset(self, **kwargs: Any) -> RunStatus:
        """Start a fresh run without carrying checks or progress over from the last one."""
        with self._lock:
            return with_home_lock(self.home, "status", lambda: self._save(kwargs, reset=True))

    def _save(self, values: dict[str, Any], *, reset: bool = False) -> RunStatus:
        current = RunStatus() if reset else self._read_current()
        for key, value in values.items():
            if key in RunStatus.__dataclass_fields__:
                setattr(current, key, value)
        if (
            type(current.runner_pid) is not int
            or current.runner_pid <= 0
            or current.runner_pid.bit_length() > 31
        ):
            current.runner_pid = None
        current.updated_at = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.path, current.to_dict())
        self.current = current
        return current

    def _read_current(self) -> RunStatus:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.current
        if not isinstance(data, dict):
            return self.current
        defaults = RunStatus().to_dict()
        values: dict[str, Any] = {}
        for key, default in defaults.items():
            value = data.get(key, default)
            if default is None:
                if type(value) is not int or value < 0:
                    value = None
                elif key == "runner_pid" and (value == 0 or value.bit_length() > 31):
                    # os.kill raises OverflowError for values outside pid_t.
                    value = None
            elif isinstance(default, float):
                value = value if type(value) in (int, float) else default
            elif type(value) is not type(default):
                value = default
            values[key] = value
        return RunStatus(**values)

    def read(self) -> RunStatus:
        with self._lock:
            self.current = self._read_current()
            return self.current

    def snapshot(self) -> dict[str, Any]:
        return self.read().to_dict()


def request_halt(home: Path, pid: int) -> Path:
    path = Path(home) / HALT_REQUEST
    with_home_lock(
        home,
        "halt",
        lambda: atomic_write_json(path, {
            "pid": int(pid),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }),
    )
    return path


def clear_halt(home: Path) -> None:
    with_home_lock(home, "halt", lambda: _clear_halt(home))


def _clear_halt(home: Path) -> None:
    path = Path(home) / HALT_REQUEST
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def halt_requested(home: Path, pid: int) -> bool:
    return with_home_lock(home, "halt", lambda: _consume_halt(home, pid))


def _consume_halt(home: Path, pid: int) -> bool:
    path = Path(home) / HALT_REQUEST
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("halt request must be an object")
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
    if type(runner_pid) is not int or runner_pid <= 0:
        return False
    me = os.getpid() if self_pid is None else self_pid
    if runner_pid == me:
        return False
    try:
        os.kill(runner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True
