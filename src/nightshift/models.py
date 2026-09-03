"""Frozen brief, upgrades, check results. After freeze, the list cannot grow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


class FrozenBriefError(RuntimeError):
    """Raised when the freeze contract is broken."""


BRIEF_SIZE_MIN = 2
BRIEF_SIZE_MAX = 5
BRIEF_SIZE_DEFAULT = 2


def clamp_brief_size(n: int) -> int:
    try:
        size = int(n)
    except (TypeError, ValueError) as exc:
        raise FrozenBriefError(f"brief size must be an integer, got {n!r}") from exc
    if size < BRIEF_SIZE_MIN or size > BRIEF_SIZE_MAX:
        raise FrozenBriefError(
            f"brief size must be {BRIEF_SIZE_MIN}-{BRIEF_SIZE_MAX}, got {size}"
        )
    return size


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
    void: bool = False
    void_reason: str = ""
    void_by: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Upgrade":
        return cls(
            id=int(data["id"]),
            title=str(data["title"]),
            check_command=str(data["check_command"]),
            paths=coerce_paths(data.get("paths")),
            done=bool(data.get("done", False)),
            note=str(data.get("note") or ""),
            void=bool(data.get("void", False)),
            void_reason=str(data.get("void_reason") or ""),
            void_by=int(data.get("void_by") or 0),
        )


def coerce_paths(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw if str(p).strip()]
    raise FrozenBriefError("paths must be a list of strings")


def coerce_check_command(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        return " ".join(str(x) for x in raw)
    return str(raw)


@dataclass
class Brief:
    upgrades: tuple[Upgrade, ...]
    frozen: bool = True
    created_at: str = ""
    repo: str = ""
    branch: str = ""
    base_ref: str = ""
    base_sha: str = ""

    def __post_init__(self) -> None:
        if self.frozen:
            clamp_brief_size(len(self.upgrades))
        object.__setattr__(self, "upgrades", tuple(self.upgrades))

    @property
    def remaining_count(self) -> int:
        return sum(1 for u in self.upgrades if not u.done and not u.void)

    def remaining(self) -> list[Upgrade]:
        return [u for u in self.upgrades if not u.done and not u.void]

    @property
    def void_count(self) -> int:
        return sum(1 for u in self.upgrades if u.void)

    def allowed_paths(self) -> set[str]:
        out: set[str] = set()
        for u in self.upgrades:
            for p in u.paths:
                out.add(normalize_rel(p))
        return out

    def add_upgrade(self, upgrade: Upgrade) -> None:
        raise FrozenBriefError("brief is frozen; cannot add another upgrade")

    def void_upgrade(self, id: int, reason: str, by: int = 0) -> None:
        """Mark an upgrade void. Cannot un-void. Cannot void if already done."""
        for u in self.upgrades:
            if u.id != id:
                continue
            if u.void:
                raise FrozenBriefError(f"upgrade {id} is already void; cannot un-void")
            if u.done:
                raise FrozenBriefError(f"upgrade {id} is already done; cannot void")
            u.void = True
            u.void_reason = str(reason or "")
            u.void_by = int(by or 0)
            return
        raise FrozenBriefError(f"upgrade {id} not found")

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
        payload: dict[str, Any] = {
            "frozen": True,
            "created_at": self.created_at,
            "repo": self.repo,
            "branch": self.branch,
            "remaining_count": self.remaining_count,
            "void_count": self.void_count,
            "upgrades": [u.to_dict() for u in self.upgrades],
        }
        if self.base_ref:
            payload["base_ref"] = self.base_ref
        if self.base_sha:
            payload["base_sha"] = self.base_sha
        return payload

    @classmethod
    def freeze(
        cls,
        upgrades: list[Upgrade],
        *,
        repo: str = "",
        branch: str = "",
        created_at: str | None = None,
        base_ref: str = "",
        base_sha: str = "",
    ) -> "Brief":
        clamp_brief_size(len(upgrades))
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
                    void=False,
                    void_reason="",
                    void_by=0,
                )
            )
        return cls(
            upgrades=tuple(items),
            frozen=True,
            created_at=stamped,
            repo=repo,
            branch=branch,
            base_ref=base_ref,
            base_sha=base_sha,
        )

    @classmethod
    def from_proposed(
        cls,
        data: dict[str, Any],
        *,
        repo: str = "",
        branch: str = "",
        size: int = BRIEF_SIZE_DEFAULT,
    ) -> "Brief":
        raw = data.get("upgrades")
        if not isinstance(raw, list):
            raise FrozenBriefError("brief JSON must have an upgrades list")
        want = clamp_brief_size(size)
        if len(raw) != want:
            raise FrozenBriefError(
                f"brief must be exactly {want} items, got {len(raw)}"
            )
        items = []
        for i, row in enumerate(raw, 1):
            if not isinstance(row, dict):
                raise FrozenBriefError("each upgrade must be an object")
            items.append(
                Upgrade(
                    id=i,
                    title=str(row.get("title") or f"upgrade {i}"),
                    check_command=coerce_check_command(
                        row.get("check_command") or row.get("check") or ""
                    ),
                    paths=coerce_paths(row.get("paths")),
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
            base_ref=str(data.get("base_ref") or ""),
            base_sha=str(data.get("base_sha") or ""),
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
    refused: list[str] = field(default_factory=list)
