"""Persistent night ledger: prior upgrades so freeze can void duplicates."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .models import Brief, Upgrade, normalize_rel

LEDGER_REL = ".nightshift/ledger.json"
LEDGER_SNAPSHOT_MAX = 8 * 1024
_TOKEN = re.compile(r"[a-z0-9]+")


def normalize_check_command(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def check_hash(cmd: str) -> str:
    """sha256 truncated to 12 hex chars of the normalized check command."""
    return hashlib.sha256(normalize_check_command(cmd).encode("utf-8")).hexdigest()[:12]


def _path_set(paths: Any) -> set[str]:
    if not isinstance(paths, (list, tuple, set, frozenset)):
        return set()
    return {normalize_rel(p) for p in paths if isinstance(p, str) and p.strip()}


def _source_paths(paths: list[str] | None) -> frozenset[str]:
    out: set[str] = set()
    for rel in _path_set(paths):
        if not rel:
            continue
        if rel.startswith("tests/") or Path(rel).name.startswith("test_"):
            continue
        out.add(rel)
    return frozenset(out)


def _title_tokens(title: str) -> set[str]:
    return set(_TOKEN.findall((title or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _near_dup_enabled() -> bool:
    return os.environ.get("NIGHTSHIFT_NEAR_DUP", "1").strip() not in {"0", "false", "no", "off"}


def _home_enabled() -> bool:
    return os.environ.get("NIGHTSHIFT_LEDGER_HOME", "1").strip() not in {"0", "false", "no", "off"}


def repo_id(repo: Path) -> str:
    """12 hex chars of sha1(resolved path). Same stem as the home ledger shard."""
    return hashlib.sha1(str(Path(repo).resolve()).encode("utf-8")).hexdigest()[:12]


def pathset_hash(paths: list[str]) -> str:
    joined = "\0".join(sorted(_path_set(paths)))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def night_id(repo_id_: str, night: str) -> str:
    slug = str(night or "").replace("/", "-")
    return f"n-{repo_id_}-{slug}"


def item_id(repo_id_: str, check_hash_: str, paths: list[str]) -> str:
    return f"i-{repo_id_}-{check_hash_}-{pathset_hash(paths)}"


def _home_path(home: Path, repo: Path) -> Path:
    return Path(home) / "ledger" / f"{repo_id(repo)}.json"


def _read_ledger_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": []}
    if not isinstance(data, dict):
        return {"entries": []}
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    else:
        data["entries"] = [entry for entry in data["entries"] if isinstance(entry, dict)]
    return data


def _write_ledger_file(path: Path, data: dict[str, Any]) -> None:
    from .forum import atomic_write_json

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    if not isinstance(payload.get("entries"), list):
        payload["entries"] = []
    atomic_write_json(path, payload)


def _merge_entry_lists(*lists: list[Any]) -> list[dict[str, Any]]:
    index: dict[tuple[str, frozenset[str]], dict[str, Any]] = {}
    order: list[tuple[str, frozenset[str]]] = []
    for entries in lists:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = _entry_key(entry)
            prev = index.get(key)
            if prev is None:
                index[key] = dict(entry)
                order.append(key)
                continue
            night = str(entry.get("night") or "")
            prev_night = str(prev.get("night") or "")
            if night >= prev_night:
                index[key] = dict(entry)
    return [index[k] for k in order]


def load_ledger(repo: Path | str, *, home: Path | None = None) -> dict[str, Any]:
    repo_p = Path(repo)
    repo_data = _read_ledger_file(repo_p / LEDGER_REL)
    if home is None or not _home_enabled():
        return repo_data
    home_data = _read_ledger_file(_home_path(Path(home), repo_p))
    return {
        "entries": _merge_entry_lists(
            list(repo_data.get("entries") or []),
            list(home_data.get("entries") or []),
        )
    }


def save_ledger(repo: Path | str, data: dict[str, Any], *, home: Path | None = None) -> Path:
    repo_p = Path(repo)
    path = repo_p / LEDGER_REL
    _write_ledger_file(path, data)
    if home is not None and _home_enabled():
        _write_ledger_file(_home_path(Path(home), repo_p), data)
    return path


def _entry_key(entry: dict[str, Any]) -> tuple[str, frozenset[str]]:
    h = str(entry.get("check_hash") or "") or check_hash(str(entry.get("check_command") or ""))
    return h, frozenset(_path_set(entry.get("paths") or []))


def history_match(upgrade: Upgrade, ledger: dict[str, Any]) -> dict[str, Any] | None:
    """Best prior entry for this upgrade: exact key, else near-duplicate."""
    want = (check_hash(upgrade.check_command), frozenset(_path_set(upgrade.paths)))
    exact: dict[str, Any] | None = None
    near: dict[str, Any] | None = None
    src = _source_paths(upgrade.paths)
    tokens = _title_tokens(upgrade.title)
    for entry in ledger.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("attempted"):
            continue
        if _entry_key(entry) == want:
            exact = entry
            break
        if not _near_dup_enabled() or not src:
            continue
        if _source_paths(entry.get("paths") or []) != src:
            continue
        if _jaccard(tokens, _title_tokens(str(entry.get("title") or ""))) >= 0.5:
            near = near or entry
    return exact or near


def is_history_duplicate(upgrade: Upgrade, ledger: dict[str, Any]) -> bool:
    """True iff some entry has same check_hash AND same paths set AND attempted=True.

    Retry is allowed when the prior night never reached host_check (attempted=False).
    """
    want = (check_hash(upgrade.check_command), frozenset(_path_set(upgrade.paths)))
    for entry in ledger.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("attempted"):
            continue
        if _entry_key(entry) == want:
            return True
    return False


def history_void_reason(upgrade: Upgrade, ledger: dict[str, Any]) -> str | None:
    found = history_match(upgrade, ledger)
    if found is None:
        return None
    if found.get("done"):
        return "duplicate_of_history"
    night = str(found.get("night") or "prior")
    note = str(found.get("note") or "")
    reason = f"failed_before:{night}"
    if note:
        reason = f"{reason} {note}"
    return reason


def merge_night_into_ledger(
    ledger: dict[str, Any],
    brief: Brief,
    night_branch: str,
    turns_by_id: dict[int, int] | None = None,
    *,
    attempted_ids: set[int] | None = None,
    last_exit_by_id: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Upsert each upgrade by check_hash + paths set."""
    entries: list[dict[str, Any]] = [
        e for e in (ledger.get("entries") or []) if isinstance(e, dict)
    ]
    index: dict[tuple[str, frozenset[str]], int] = {}
    for i, entry in enumerate(entries):
        index[_entry_key(entry)] = i
    for upgrade in brief.upgrades:
        h = check_hash(upgrade.check_command)
        paths = [normalize_rel(str(p)) for p in (upgrade.paths or []) if str(p).strip()]
        key = (h, frozenset(paths))
        found = entries[index[key]] if key in index else None
        if attempted_ids is not None:
            attempted = (upgrade.id in attempted_ids) or bool(found and found.get("attempted"))
        else:
            attempted = (not upgrade.void) or bool(found and found.get("attempted"))
        if turns_by_id is not None and upgrade.id in turns_by_id:
            turns = int(turns_by_id[upgrade.id])
        elif found and found.get("turns") is not None:
            turns = int(found.get("turns") or 1)
        else:
            turns = 1
        last_exit = 0
        if last_exit_by_id is not None and upgrade.id in last_exit_by_id:
            try:
                last_exit = int(last_exit_by_id[upgrade.id])
            except (TypeError, ValueError):
                last_exit = 0
        elif found and not upgrade.done:
            try:
                last_exit = int(found.get("last_exit") or 0)
            except (TypeError, ValueError):
                last_exit = 0
        if found and found.get("done") and not upgrade.done:
            # Do not clobber a landed history row with a later void/duplicate.
            continue
        row = {
            "title": upgrade.title,
            "check_command": upgrade.check_command,
            "paths": paths,
            "check_hash": h,
            "night": night_branch,
            "attempted": bool(attempted),
            "done": bool(upgrade.done),
            "voided": bool(upgrade.void),
            "void_reason": str(upgrade.void_reason or ""),
            "last_exit": last_exit,
            "turns": turns,
            "note": str(upgrade.note or (found or {}).get("note") or ""),
        }
        if found and found.get("base"):
            row["base"] = found.get("base")
        if key in index:
            entries[index[key]] = row
        else:
            index[key] = len(entries)
            entries.append(row)
    ledger["entries"] = entries
    return ledger


def ledger_snapshot_block(ledger: dict[str, Any]) -> str:
    """Markdown ≤8KB of prior titles/checks for the critic."""
    entries = [e for e in (ledger.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return ""
    lines = [
        "## Prior night ledger",
        "",
        "Do not re-propose these unless the check changed.",
        "",
    ]
    for entry in entries:
        flags: list[str] = []
        if entry.get("done"):
            flags.append("done")
        if entry.get("voided"):
            flags.append("voided")
            if not entry.get("done"):
                flags.append("failed")
        if entry.get("attempted"):
            flags.append("attempted")
        turns = entry.get("turns")
        if turns and not entry.get("done"):
            flags.append(f"{int(turns)} turns")
        mark = ",".join(flags) or "open"
        title = str(entry.get("title") or "(untitled)")
        cmd = str(entry.get("check_command") or "")
        note = str(entry.get("note") or "")
        line = f"- [{mark}] {title}"
        if note:
            line += f" -- {note}"
        lines.append(line)
        if cmd:
            lines.append(f"  check: `{cmd}`")
    text = "\n".join(lines) + "\n"
    raw = text.encode("utf-8")
    if len(raw) <= LEDGER_SNAPSHOT_MAX:
        return text
    cut = raw[:LEDGER_SNAPSHOT_MAX]
    truncated = cut.decode("utf-8", errors="ignore").rstrip()
    return truncated + "\n"
