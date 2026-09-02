"""Frozen brief, upgrades, check results. After freeze, there is no fourth item."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


class FrozenBriefError(RuntimeError):
    """Raised when a fourth upgrade is proposed or the freeze contract is broken."""


class SafetyError(RuntimeError):
    """Refused to run: path, branch, or tool-role contract."""


def normalize_rel(rel: str) -> str:
    """Normalize a repo-relative path. Do not use str.lstrip('./') — that strips dots."""
    norm = rel.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


@dataclass
class Upgrade:
    id: int
    title: str
    check_command: str
    paths: list[str] = field(default_factory=list)
    done: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Upgrade":
        return cls(
            id=int(data["id"]),
            title=str(data["title"]),
            check_command=str(data["check_command"]),
            paths=[str(p) for p in data.get("paths") or []],
            done=bool(data.get("done", False)),
            note=str(data.get("note") or ""),
        )


@dataclass
class Brief:
    upgrades: tuple[Upgrade, ...]
    frozen: bool = True
    created_at: str = ""
    repo: str = ""
    branch: str = ""

    def __post_init__(self) -> None:
        if self.frozen and len(self.upgrades) != 3:
            raise FrozenBriefError(
                f"brief must contain exactly 3 upgrades, got {len(self.upgrades)}"
            )
        object.__setattr__(self, "upgrades", tuple(self.upgrades))

    @property
    def remaining_count(self) -> int:
        return sum(1 for u in self.upgrades if not u.done)

    def remaining(self) -> list[Upgrade]:
        return [u for u in self.upgrades if not u.done]

    def allowed_paths(self) -> set[str]:
        out: set[str] = set()
        for u in self.upgrades:
            for p in u.paths:
                out.add(normalize_rel(p))
        return out

    def add_upgrade(self, upgrade: Upgrade) -> None:
        raise FrozenBriefError(
            "brief is frozen at 3 upgrades; cannot add a fourth"
        )

    def mark_done(self, ids: Iterable[int]) -> None:
        want = set(ids)
        for u in self.upgrades:
            if u.id in want:
                u.done = True

    def mark_not_done(self, ids: Iterable[int]) -> None:
        want = set(ids)
        for u in self.upgrades:
            if u.id in want:
                u.done = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "frozen": True,
            "created_at": self.created_at,
            "repo": self.repo,
            "branch": self.branch,
            "remaining_count": self.remaining_count,
            "upgrades": [u.to_dict() for u in self.upgrades],
        }

    @classmethod
    def freeze(
        cls,
        upgrades: list[Upgrade],
        *,
        repo: str = "",
        branch: str = "",
        created_at: str | None = None,
    ) -> "Brief":
        if len(upgrades) != 3:
            raise FrozenBriefError(
                f"brief must contain exactly 3 upgrades, got {len(upgrades)}"
            )
        stamped = created_at or datetime.now(timezone.utc).isoformat()
        items = []
        for i, u in enumerate(upgrades, 1):
            items.append(
                Upgrade(
                    id=i,
                    title=u.title,
                    check_command=u.check_command,
                    paths=list(u.paths),
                    done=False,
                    note=u.note,
                )
            )
        return cls(
            upgrades=tuple(items),
            frozen=True,
            created_at=stamped,
            repo=repo,
            branch=branch,
        )

    @classmethod
    def from_proposed(
        cls,
        data: dict[str, Any],
        *,
        repo: str = "",
        branch: str = "",
    ) -> "Brief":
        raw = data.get("upgrades")
        if not isinstance(raw, list):
            raise FrozenBriefError("brief JSON must have an upgrades list")
        if len(raw) != 3:
            raise FrozenBriefError(
                f"fourth upgrade rejected; brief must be exactly 3 items, got {len(raw)}"
            )
        items = []
        for i, row in enumerate(raw, 1):
            if not isinstance(row, dict):
                raise FrozenBriefError("each upgrade must be an object")
            items.append(
                Upgrade(
                    id=i,
                    title=str(row.get("title") or f"upgrade {i}"),
                    check_command=str(row.get("check_command") or row.get("check") or ""),
                    paths=[str(p) for p in (row.get("paths") or [])],
                    done=False,
                    note=str(row.get("note") or ""),
                )
            )
        for u in items:
            if not u.check_command.strip():
                raise FrozenBriefError(f"upgrade {u.id} has no check command")
            if not u.title.strip():
                raise FrozenBriefError(f"upgrade {u.id} has no title")
        return cls.freeze(items, repo=repo, branch=branch)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Brief":
        items = [Upgrade.from_dict(u) for u in data.get("upgrades") or []]
        return cls(
            upgrades=tuple(items),
            frozen=bool(data.get("frozen", True)),
            created_at=str(data.get("created_at") or ""),
            repo=str(data.get("repo") or ""),
            branch=str(data.get("branch") or ""),
        )


@dataclass
class CheckResult:
    upgrade_id: int
    command: str
    ok: bool
    exit_code: int
    output: str


@dataclass
class WriterResult:
    written: list[str]
    message: str
    raw: str = ""
