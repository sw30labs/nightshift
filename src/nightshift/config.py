"""Env-first settings. Live Mac/Spark URLs are the defaults; mock is opt-in."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


def _split_roots(raw: str | None) -> list[Path]:
    if not raw:
        return [Path.home() / "REPOS"]
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


@dataclass
class Settings:
    writer_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "NIGHTSHIFT_WRITER_BASE_URL", "http://192.168.86.44:8000/v1"
        )
    )
    writer_model: str = field(
        default_factory=lambda: os.environ.get(
            "NIGHTSHIFT_WRITER_MODEL", "deepseek-v4-flash"
        )
    )
    critic_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "NIGHTSHIFT_CRITIC_BASE_URL", "http://127.0.0.1:8000/v1"
        )
    )
    critic_model: str = field(
        default_factory=lambda: os.environ.get(
            "NIGHTSHIFT_CRITIC_MODEL", "GLM-5.3-Flash-MLX-8bit"
        )
    )
    api_key: str = field(
        default_factory=lambda: os.environ.get("NIGHTSHIFT_API_KEY", "test")
    )
    roots: list[Path] = field(
        default_factory=lambda: _split_roots(os.environ.get("NIGHTSHIFT_ROOTS"))
    )
    halt_at: str = field(
        default_factory=lambda: os.environ.get("NIGHTSHIFT_HALT_AT", "06:00")
    )
    max_turns: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_MAX_TURNS", "20"))
    )
    check_timeout: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_CHECK_TIMEOUT", "120"))
    )
    mock: bool = field(
        default_factory=lambda: os.environ.get("NIGHTSHIFT_MOCK", "").strip()
        in {"1", "true", "yes", "on"}
    )
    push: bool = False
    include_deprecated: bool = False
    observe: bool = True
    open_browser: bool = False
    loopscope_port: int = 7788
    deck_host: str = "127.0.0.1"
    home: Path = field(
        default_factory=lambda: Path(
            os.environ.get("NIGHTSHIFT_HOME", Path.home() / ".nightshift")
        ).expanduser()
    )
    now_fn: Callable[[], datetime] = datetime.now
    halt_deadline: datetime | None = None
    stall_after: int = 8
    writer_timeout: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_WRITER_TIMEOUT", "600"))
    )
    critic_timeout: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_CRITIC_TIMEOUT", "180"))
    )
    brief_size: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_BRIEF_SIZE", "2"))
    )
    job_turns: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_JOB_TURNS", "4"))
    )
    allow_dirty: bool = field(
        default_factory=lambda: os.environ.get("NIGHTSHIFT_ALLOW_DIRTY", "").strip()
        in {"1", "true", "yes", "on"}
    )
    dry_run: bool = False
    deck_port: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_PORT", "43171"))
    )
    bag_size: int = field(
        default_factory=lambda: int(os.environ.get("NIGHTSHIFT_BAG_SIZE", "2"))
    )
    skip_meta: bool = False
    meta_last: bool = False
    bag_min_minutes: int = field(
        default_factory=lambda: max(
            0, int(os.environ.get("NIGHTSHIFT_BAG_MIN_MINUTES", "30") or "30")
        )
    )
    forum_enabled: bool = field(
        default_factory=lambda: os.environ.get("NIGHTSHIFT_FORUM", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    bag_id: str = ""

    def state_dir(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home

    @classmethod
    def from_cli(
        cls,
        *,
        roots: Sequence[str] | None = None,
        mock: bool | None = None,
        push: bool = False,
        halt_at: str | None = None,
        max_turns: int | None = None,
        brief_size: int | None = None,
        include_deprecated: bool = False,
        observe: bool = True,
        host: str | None = None,
        port: int | None = None,
        allow_dirty: bool = False,
        dry_run: bool = False,
        job_turns: int | None = None,
        bag_size: int | None = None,
        skip_meta: bool = False,
        meta_last: bool = False,
    ) -> "Settings":
        s = cls()
        if roots:
            s.roots = [Path(p).expanduser() for p in roots]
        if mock is not None:
            s.mock = mock
        s.push = push
        if halt_at:
            s.halt_at = halt_at
        if max_turns is not None:
            s.max_turns = max_turns
        if brief_size is not None:
            s.brief_size = brief_size
        s.include_deprecated = include_deprecated
        s.observe = observe
        if host:
            s.deck_host = host
        if port is not None:
            s.deck_port = port
        if allow_dirty:
            s.allow_dirty = True
        s.dry_run = bool(dry_run)
        if job_turns is not None:
            s.job_turns = int(job_turns)
        if bag_size is not None:
            s.bag_size = bag_size
        if skip_meta:
            s.skip_meta = True
        if meta_last:
            s.meta_last = True
        from .models import clamp_brief_size

        s.brief_size = clamp_brief_size(s.brief_size)
        if s.job_turns < 1:
            s.job_turns = 1
        try:
            s.bag_size = max(1, min(3, int(s.bag_size)))
        except (TypeError, ValueError):
            s.bag_size = 2
        try:
            s.bag_min_minutes = max(0, int(s.bag_min_minutes))
        except (TypeError, ValueError):
            s.bag_min_minutes = 30
        return s
