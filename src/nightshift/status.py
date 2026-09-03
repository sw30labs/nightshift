"""Live run status for the CLI and the command deck."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
