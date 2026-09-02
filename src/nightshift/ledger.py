"""Persistent night ledger: prior upgrades so freeze can void duplicates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import Brief, Upgrade, normalize_rel

LEDGER_REL = ".nightshift/ledger.json"
LEDGER_SNAPSHOT_MAX = 8 * 1024


def normalize_check_command(cmd: str) -> str:
    return " ".join(str(cmd or "").split())


def check_hash(cmd: str) -> str:
    """sha256 truncated to 12 hex chars of the normalized check command."""
    return hashlib.sha256(normalize_check_command(cmd).encode("utf-8")).hexdigest()[:12]


def _path_set(paths: list[str] | None) -> set[str]:
    return {normalize_rel(str(p)) for p in (paths or []) if str(p).strip()}


def load_ledger(repo: Path | str) -> dict[str, Any]:
    path = Path(repo) / LEDGER_REL
    if not path.is_file():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(data, dict):
        return {"entries": []}
    entries = data.get("entries")
    if not isinstance(entries, list):
        data["entries"] = []
    return data


def save_ledger(repo: Path | str, data: dict[str, Any]) -> Path:
    path = Path(repo) / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data or {})
    if not isinstance(payload.get("entries"), list):
        payload["entries"] = []
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _entry_key(entry: dict[str, Any]) -> tuple[str, frozenset[str]]:
    h = str(entry.get("check_hash") or "") or check_hash(str(entry.get("check_command") or ""))
    return h, frozenset(_path_set(entry.get("paths") or []))


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


def merge_night_into_ledger(
    ledger: dict[str, Any],
    brief: Brief,
    night_branch: str,
    turns_by_id: dict[int, int] | None = None,
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
        attempted = (not upgrade.void) or bool(found and found.get("attempted"))
        if turns_by_id is not None and upgrade.id in turns_by_id:
            turns = int(turns_by_id[upgrade.id])
        elif found and found.get("turns") is not None:
            turns = int(found.get("turns") or 1)
        else:
            turns = 1
        last_exit = 0
        if found and not upgrade.done:
            try:
                last_exit = int(found.get("last_exit") or 0)
            except (TypeError, ValueError):
                last_exit = 0
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
        }
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
        if entry.get("attempted"):
            flags.append("attempted")
        mark = ",".join(flags) or "open"
        title = str(entry.get("title") or "(untitled)")
        cmd = str(entry.get("check_command") or "")
        lines.append(f"- [{mark}] {title}")
        if cmd:
            lines.append(f"  check: `{cmd}`")
    text = "\n".join(lines) + "\n"
    raw = text.encode("utf-8")
    if len(raw) <= LEDGER_SNAPSHOT_MAX:
        return text
    cut = raw[:LEDGER_SNAPSHOT_MAX]
    truncated = cut.decode("utf-8", errors="ignore").rstrip()
    return truncated + "\n"
