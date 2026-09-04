"""Shared forum: portfolio-grain ledger at ~/.nightshift/forum.json + forum.md.

Data layer only. Nights and items are projected from ledger rows (ingest) or,
later, from a NightReport (publish). Never from the writer. Never at freeze.
Ingest is a latest-entry projection: `load_ledger(repo, home=)` keeps one row
per (check_hash, paths) — historical nights for the same key are gone.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .gitops import git
from .ledger import LEDGER_REL, _entry_key, check_hash, item_id, load_ledger, night_id, repo_id
from .models import SafetyError, normalize_rel
from .observe import log
from .safety import is_blocked_rel, is_nightshift_repo, resolve_repo

FORUM_SCHEMA = 1
FORUM_REL = "forum.json"
FORUM_MD_REL = "forum.md"
BAG_REL = "bag.json"
BRIEF_REL = ".nightshift/brief.json"
TEXT_MAX = 500
MERGED_BY_GIT = "git"
MERGED_BY_OPERATOR = "operator"
_LIST_KEYS = ("nights", "items", "reuse_events", "errors")
_NIGHT_DATE = re.compile(r"night/(\d{4}-\d{2}-\d{2})")
_T = TypeVar("_T")
_EntryKey = tuple[str, frozenset[str]]


def forum_enabled() -> bool:
    return os.environ.get("NIGHTSHIFT_FORUM", "1").strip().lower() not in {"0", "false", "no", "off"}


def empty_forum() -> dict[str, Any]:
    return {
        "schema": FORUM_SCHEMA,
        "updated_at": "",
        "nights": [],
        "items": [],
        "reuse_events": [],
        "errors": [],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip(text: Any, limit: int = TEXT_MAX) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_paths(paths: Any) -> list[str]:
    """Sorted, deduped, repo-relative, secrets filtered. Never absolute."""
    out: set[str] = set()
    for p in paths if isinstance(paths, (list, tuple)) else []:
        rel = normalize_rel(str(p))
        if not rel or is_blocked_rel(rel):
            continue
        out.add(rel)
    return sorted(out)


def _str_list(value: Any) -> list[str]:
    """A stored list-of-str field: str entries kept; a scalar, None or junk entry reads as absent."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, (list, tuple)) else []


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    """A stored row list: dict rows kept; a scalar or non-dict entry reads as absent."""
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


# --- lock + atomic write -----------------------------------------------------


def with_home_lock(home: Path, name: str, fn: Callable[[], _T]) -> _T:
    """flock LOCK_EX on home/<name>.lock around fn(); unlock in finally."""
    lock_path = Path(home) / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fn()
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write path.tmp then os.replace. A crash leaves *.tmp, never a torn JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- load / save -------------------------------------------------------------


def _coerce_forum(data: dict[str, Any]) -> dict[str, Any] | None:
    """Default missing keys in place; keep unknown keys. None if schema is unusable."""
    if "schema" not in data:
        data["schema"] = FORUM_SCHEMA
    schema = _as_int(data.get("schema"), 0)
    if schema < 1:
        return None
    data["schema"] = schema
    if not isinstance(data.get("updated_at"), str):
        data["updated_at"] = ""
    for key in _LIST_KEYS:
        data[key] = _dict_rows(data.get(key))
    # Per-row list fields every reader iterates. A scalar here (hand edit, torn
    # merge) must not poison every later upsert / render until an operator
    # repairs the file by hand. Absent stays absent; readers .get() tolerantly.
    for item in data["items"]:
        if "paths" in item:
            item["paths"] = _str_list(item["paths"])
    for night in data["nights"]:
        if "item_ids" in night:
            night["item_ids"] = _str_list(night["item_ids"])
    return data


def _coerce_or_raise(data: dict[str, Any]) -> dict[str, Any]:
    payload = _coerce_forum(data)
    if payload is None:
        raise SafetyError(f"refusing to save forum with schema {data.get('schema')!r}")
    return payload


def load_forum(home: Path) -> dict[str, Any]:
    """Tolerant: missing / corrupt / non-dict -> empty document. Never raises."""
    path = Path(home) / FORUM_REL
    if not path.is_file():
        return empty_forum()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_forum()
    if not isinstance(data, dict):
        return empty_forum()
    return _coerce_forum(data) or empty_forum()


def _write_forum(home_p: Path, payload: dict[str, Any]) -> Path:
    """Stamp updated_at, write forum.json atomically, regenerate forum.md. Caller holds forum.lock."""
    path = home_p / FORUM_REL
    payload["updated_at"] = _utc_now()
    # Render first: a render failure then leaves forum.json and forum.md both
    # as they were, never a new forum.json beside a stale forum.md.
    text = render_forum_md(payload, home=home_p)
    atomic_write_json(path, payload)
    _atomic_write_text(home_p / FORUM_MD_REL, text)
    return path


def save_forum(home: Path, data: dict[str, Any]) -> Path:
    """Write a whole document under forum.lock.

    For callers that own the entire document. A read-modify-write must go
    through `mutate_forum` so the load happens inside the same critical section.
    """
    home_p = Path(home)
    payload = _coerce_or_raise(data)
    return with_home_lock(home_p, "forum", lambda: _write_forum(home_p, payload))


def mutate_forum(
    home: Path,
    fn: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    """load -> fn(forum) -> save, all under forum.lock. Returns the saved forum.

    `fn` edits the loaded document in place (or returns a replacement). Two
    writers — a night's publish, a morning ingest, an operator mark-merged —
    cannot lose each other's update. A raise inside `fn` writes nothing.
    """
    home_p = Path(home)

    def _go() -> dict[str, Any]:
        forum = load_forum(home_p)
        out = fn(forum)
        payload = _coerce_or_raise(out if isinstance(out, dict) else forum)
        _write_forum(home_p, payload)
        return payload

    return with_home_lock(home_p, "forum", _go)


# --- keys + upserts ----------------------------------------------------------


def night_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("repo_id") or ""), str(row.get("night") or "")


def item_key(row: dict[str, Any]) -> tuple[str, str, frozenset[str]]:
    """ledger._entry_key plus repo_id. A scalar or junk `paths` keys as no paths, never raises."""
    h, paths = _entry_key({**row, "paths": _str_list(row.get("paths"))})
    return str(row.get("repo_id") or ""), h, paths


def reuse_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("origin_item_id") or ""),
        str(row.get("consumer_item_id") or ""),
        str(row.get("consumer_night") or ""),
        str(row.get("kind") or ""),
    )


def upsert_night(forum: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Upsert by (repo_id, night).

    Only a git-stamped merged=True (merged_by == "git") is recomputed, and only
    by an incoming row that carries a `merged` verdict: stale git evidence is
    never permanent. Any other stored merged=True — an operator mark-merged,
    or a stamp of unknown origin — is sticky: it keeps both `merged` and its
    `merged_by` whatever the incoming row says, so §7 rule (3) alone keeps the
    night merged even when git evidence appears and later vanishes.
    """
    rows = forum.setdefault("nights", [])
    want = night_key(row)
    for i, stored in enumerate(rows):
        if night_key(stored) != want:
            continue
        merged = dict(stored)
        merged.update(row)
        if stored.get("merged") is True:
            stamp = str(stored.get("merged_by") or "")
            recomputed = stamp == MERGED_BY_GIT and "merged" in row
            if not recomputed:
                merged["merged"] = True
                merged["merged_by"] = stamp
        rows[i] = merged
        return merged
    rows.append(row)
    return row


def upsert_item(forum: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Upsert by (repo_id, check_hash, frozenset(paths)).

    Do not clobber a stored done=true row with a later non-done row (same rule
    as merge_night_into_ledger): keep it, fill an empty note, raise turns.
    """
    rows = forum.setdefault("items", [])
    want = item_key(row)
    for i, stored in enumerate(rows):
        if item_key(stored) != want:
            continue
        if stored.get("done") and not row.get("done"):
            if not stored.get("note") and row.get("note"):
                stored["note"] = clip(row.get("note"))
            if _as_int(row.get("turns")) > _as_int(stored.get("turns")):
                stored["turns"] = _as_int(row.get("turns"))
            return stored
        merged = dict(stored)
        merged.update(row)
        if stored.get("attempted"):
            merged["attempted"] = True
        rows[i] = merged
        return merged
    rows.append(row)
    return row


def upsert_reuse(forum: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    rows = forum.setdefault("reuse_events", [])
    want = reuse_key(row)
    for i, stored in enumerate(rows):
        if reuse_key(stored) == want:
            merged = dict(stored)
            merged.update(row)
            rows[i] = merged
            return merged
    rows.append(row)
    return row


# --- projection --------------------------------------------------------------


def item_from_ledger_row(
    repo_id_: str,
    repo_name: str,
    row: dict[str, Any],
    *,
    night: str = "",
    lens: str = "",
) -> dict[str, Any]:
    """Forum item from a clone-ledger row. No check output, note clipped, secrets filtered."""
    paths = _norm_paths(row.get("paths"))
    h = str(row.get("check_hash") or "") or check_hash(str(row.get("check_command") or ""))
    return {
        "id": item_id(repo_id_, h, paths),
        "repo_id": repo_id_,
        "repo_name": repo_name,
        "night": str(night or row.get("night") or ""),
        "title": clip(row.get("title")),
        "check_command": str(row.get("check_command") or ""),
        "check_hash": h,
        "paths": paths,
        "attempted": bool(row.get("attempted")),
        "done": bool(row.get("done")),
        "voided": bool(row.get("voided")),
        "void_reason": clip(row.get("void_reason")),
        "last_exit": _as_int(row.get("last_exit")),
        "turns": _as_int(row.get("turns")),
        "note": clip(row.get("note")),
        "lens": str(lens or ""),
    }


def _count_rows(rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    landed = sum(1 for r in rows if r.get("done"))
    voided = sum(1 for r in rows if r.get("voided") and not r.get("done"))
    remaining = len(rows) - landed - voided
    return landed, voided, remaining


# --- git evidence ------------------------------------------------------------


def _is_work_tree(path: Path) -> bool:
    """Same test as repos.find_repos: a .git dir, or a .git file (linked work tree)."""
    marker = Path(path) / ".git"
    return marker.is_dir() or marker.is_file()


def _ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0


def _show_json(repo: Path, spec: str) -> Any:
    """`git show <rev>:<path>` parsed as JSON; None when missing or not JSON."""
    proc = git(repo, "show", spec, check=False)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def strict_default_branch(repo: Path) -> str:
    """The trunk used for merge evidence, from explicit refs only — never HEAD.

    `gitops.default_branch` falls back to the current branch, which during and
    after a night is the night branch itself. Here: local `main`, else
    `master`, else the branch `origin/HEAD` points at when it exists locally.
    "" when none resolves — then there is no evidence, not a guess.
    """
    for name in ("main", "master"):
        if _ref_exists(repo, f"refs/heads/{name}"):
            return name
    proc = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    target = proc.stdout.strip() if proc.returncode == 0 else ""
    prefix = "refs/remotes/origin/"
    if target.startswith(prefix):
        name = target[len(prefix) :]
        if name and _ref_exists(repo, f"refs/heads/{name}"):
            return name
    return ""


def _done_keys_by_night(ledger: Any) -> dict[str, set[_EntryKey]]:
    """night -> entry keys of done=true rows in one parsed ledger document."""
    out: dict[str, set[_EntryKey]] = {}
    entries = ledger.get("entries") if isinstance(ledger, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or not e.get("done"):
            continue
        night = str(e.get("night") or "")
        if night:
            out.setdefault(night, set()).add(_entry_key(e))
    return out


def default_ledger_evidence(repo: Path, base: str) -> dict[str, set[_EntryKey]]:
    """Done rows on `base` that are there now AND provably belong to their night.

    §7 rule (1), refined (design note N10). Two conditions, both required:

    * literal: the file `<base>:.nightshift/ledger.json` as it is today holds a
      done=true row for night N. A merge that was reverted, or a trunk reset
      below the landing, drops the rows and the evidence with them.
    * provenance: some commit reachable from `base` that touched the ledger
      carries a frozen `.nightshift/brief.json` naming N next to that done
      row. The clone ledger is a clone + home-shard projection, so a dropped
      night's done rows ride into the next night's ledger commit and would
      otherwise look landed once that later night merges — the home shard is
      never merge proof.

    Returns night -> entry keys of done rows that satisfy both.
    """
    if not base:
        return {}
    current = _done_keys_by_night(_show_json(repo, f"{base}:{LEDGER_REL}"))
    if not current:
        return {}
    proc = git(repo, "log", "--full-history", "--format=%H", base, "--", LEDGER_REL, check=False)
    if proc.returncode != 0:
        return {}
    provenance: dict[str, set[_EntryKey]] = {}
    for sha in proc.stdout.split():
        brief = _show_json(repo, f"{sha}:{BRIEF_REL}")
        night = str(brief.get("branch") or "") if isinstance(brief, dict) else ""
        if not night or night not in current:
            continue
        keys = _done_keys_by_night(_show_json(repo, f"{sha}:{LEDGER_REL}")).get(night) or set()
        if keys:
            provenance.setdefault(night, set()).update(keys)
    out: dict[str, set[_EntryKey]] = {}
    for night, keys in current.items():
        both = keys & (provenance.get(night) or set())
        if both:
            out[night] = both
    return out


def night_merged(
    repo: Path,
    night: str,
    done_rows: list[dict[str, Any]],
    *,
    base: str | None = None,
    evidence: dict[str, set[_EntryKey]] | None = None,
) -> bool:
    """Git evidence only. Never HEAD-is-default, never a home shard.

    True iff (1) the default branch's ledger holds a done=true row for this
    night today and a commit on that branch carries the night's frozen brief
    next to it (one of `done_rows` when those are known; see
    default_ledger_evidence), or (2) the night branch still exists and is an
    ancestor of the default branch. `base` / `evidence` let ingest resolve the
    trunk and walk the ledger history once per repo. A path that is not a git
    work tree (clone gone) has no evidence.
    """
    if not night or not _is_work_tree(repo):
        return False
    if base is None:
        base = strict_default_branch(repo)
    if not base or base == night:
        return False
    if evidence is None:
        evidence = default_ledger_evidence(repo, base)
    wanted = {_entry_key(r) for r in done_rows if isinstance(r, dict)}
    found = evidence.get(night) or set()
    if found and (not wanted or wanted & found):
        return True
    if _ref_exists(repo, f"refs/heads/{night}"):
        if git(repo, "merge-base", "--is-ancestor", night, base, check=False).returncode == 0:
            return True
    return False


# --- ingest ------------------------------------------------------------------


@dataclass
class _NightPlan:
    """One ledger night of one repo, projected outside the forum lock."""

    path: Path
    rid: str
    meta: bool
    base_ref: str
    night: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    merged: bool = False


def _plan_repo(home_p: Path, repo: Path) -> tuple[str, list[_NightPlan]]:
    path = resolve_repo(Path(repo))
    rid = repo_id(path)
    data = load_ledger(path, home=home_p)
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        night = str(entry.get("night") or "")
        if night:
            groups.setdefault(night, []).append(entry)
    if not groups:
        return rid, []
    meta = is_nightshift_repo(path)
    base_ref = strict_default_branch(path)
    evidence = default_ledger_evidence(path, base_ref)
    plans: list[_NightPlan] = []
    for night, rows in groups.items():
        done_rows = [r for r in rows if r.get("done")]
        plans.append(
            _NightPlan(
                path=path,
                rid=rid,
                meta=meta,
                base_ref=base_ref,
                night=night,
                rows=rows,
                merged=night_merged(path, night, done_rows, base=base_ref, evidence=evidence),
            )
        )
    return rid, plans


def _apply_plan(forum: dict[str, Any], plan: _NightPlan, stored: dict[str, Any] | None) -> int:
    """Upsert one night's items and row. Returns the item count."""
    items = [item_from_ledger_row(plan.rid, plan.path.name, r, night=plan.night) for r in plan.rows]
    for item in items:
        upsert_item(forum, item)
    row: dict[str, Any] = {
        "id": night_id(plan.rid, plan.night),
        "repo_id": plan.rid,
        "night": plan.night,
        "merged": plan.merged,
        "merged_by": MERGED_BY_GIT if plan.merged else "",
    }
    if stored is None or str(stored.get("halt_reason") or "") == "ingested":
        # Only a row ingest itself created is ingest's to describe. A night
        # published live knows its clock, halt, base sha, mock and lens; the
        # morning projection may only add merge evidence to it.
        landed, voided, remaining = _count_rows(items)
        row.update(
            {
                "repo_name": plan.path.name,
                "repo_path": str(plan.path),
                "meta": plan.meta,
                "branch": plan.night,
                "started_at": "",
                "ended_at": "",
                "halt_reason": "ingested",
                "base_ref": plan.base_ref,
                "base_sha": "",
                "main_untouched": True,
                "landed": landed,
                "voided": voided,
                "remaining": remaining,
                "error": "",
                "mock": False,
                "brief_size": len(items),
                "lens_hint": "",
                "item_ids": [i["id"] for i in items],
            }
        )
    upsert_night(forum, row)
    return len(items)


def ingest_forum(
    home: Path,
    repos: list[Path],
    *,
    stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Latest-entry projection of clone + home ledgers into the forum.

    Read-only on clones and home shards. Never invents reuse_events. `merged`
    comes from git evidence only (see night_merged). A population path that
    is not a git work tree any more (clone gone, or listed before it was
    deleted) is skipped and logged, never projected: its home shard is then an
    orphan like any other. Orphan shards (stem matches no ingested repo) are
    counted and logged, never guessed. `stats["repos"]` counts ingested paths.
    Git work happens outside the forum lock; load -> upsert -> save inside it.
    Idempotent and additive on rows a live publish wrote. Safe every morning.
    """
    home_p = Path(home)
    counts = {"repos": 0, "nights": 0, "items": 0, "orphans": 0}
    seen: set[str] = set()
    plans: list[_NightPlan] = []
    gone: list[Path] = []
    for repo in repos:
        path = resolve_repo(Path(repo))
        if not _is_work_tree(path):
            gone.append(path)
            continue
        rid, repo_plans = _plan_repo(home_p, path)
        seen.add(rid)
        counts["repos"] += 1
        plans.extend(repo_plans)
    if gone:
        names = ", ".join(p.name for p in gone)
        log(f"forum ingest: {len(gone)} population path(s) not a git work tree, skipped: {names}")
    orphans = [p for p in (home_p / "ledger").glob("*.json") if p.stem not in seen]
    counts["orphans"] = len(orphans)
    if orphans:
        log(f"forum ingest: {len(orphans)} orphan home ledger shard(s) skipped, not guessed")

    def _apply(forum: dict[str, Any]) -> dict[str, Any]:
        stored_by_key = {night_key(n): n for n in forum.get("nights") or []}
        for plan in plans:
            counts["items"] += _apply_plan(forum, plan, stored_by_key.get((plan.rid, plan.night)))
            counts["nights"] += 1
        return forum

    forum = mutate_forum(home_p, _apply)
    if stats is not None:
        stats.update(counts)
    return forum


# --- operator: mark-merged ---------------------------------------------------


def _night_has_done_item(forum: dict[str, Any], night: dict[str, Any]) -> bool:
    rid, name = night_key(night)
    for item in forum.get("items") or []:
        if item.get("done") and item.get("repo_id") == rid and str(item.get("night") or "") == name:
            return True
    return False


def mark_merged(home: Path, repo: Path, night: str | None = None) -> dict[str, Any]:
    """Operator evidence for cherry-picked keepers. Sticky across ingests.

    NIGHT omitted: stamp only the most recent night for this repo with a
    done=true item and merged=False (by ended_at, then night). Never all.
    """
    rid = repo_id(repo)
    name = Path(repo).name

    def _apply(forum: dict[str, Any]) -> dict[str, Any]:
        nights = [n for n in forum.get("nights") or [] if n.get("repo_id") == rid]
        if not nights:
            raise SafetyError(f"no forum nights for {name} ({rid}); run `nightshift forum ingest` first")
        if night:
            row = next((n for n in nights if str(n.get("night") or "") == night), None)
            if row is None:
                raise SafetyError(f"no forum night {night!r} for {name} ({rid})")
        else:
            candidates = [
                n for n in nights if n.get("merged") is not True and _night_has_done_item(forum, n)
            ]
            if not candidates:
                raise SafetyError(
                    f"no unmerged forum night with a done item for {name}; pass NIGHT explicitly"
                )
            row = max(
                candidates,
                key=lambda n: (str(n.get("ended_at") or ""), str(n.get("night") or "")),
            )
        row["merged"] = True
        row["merged_by"] = MERGED_BY_OPERATOR
        return forum

    return mutate_forum(Path(home), _apply)


# --- forum.md ----------------------------------------------------------------


def _load_bag_file(home: Path) -> dict[str, Any] | None:
    """Private tolerant reader for home/bag.json. forum.py never imports bag.py."""
    path = Path(home) / BAG_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _night_date(night: dict[str, Any]) -> str:
    m = _NIGHT_DATE.search(str(night.get("night") or ""))
    if m:
        return m.group(1)
    for key in ("ended_at", "started_at"):
        stamp = str(night.get(key) or "")
        if stamp:
            return stamp[:10]
    return "????-??-??"


def _night_sort_key(night: dict[str, Any]) -> tuple[str, str, str]:
    return (_night_date(night), str(night.get("ended_at") or ""), str(night.get("night") or ""))


def _item_mark(item: dict[str, Any]) -> str:
    if item.get("done"):
        return "[done]"
    if item.get("voided"):
        return f"[void {clip(item.get('void_reason'), 120) or 'voided'}]"
    return "[open]"


def _bag_target_line(target: dict[str, Any], nights_by_key: dict[tuple[str, str], dict[str, Any]]) -> str:
    name = str(target.get("name") or target.get("repo_id") or "?")
    state = str(target.get("state") or "")
    if state == "skipped":
        reason = str(target.get("error") or target.get("halt_reason") or "skipped")
        return f"- {name}  skipped: {clip(reason, 120)}"
    branch = str(target.get("branch") or "")
    parts = [name, branch or state]
    halt = str(target.get("halt_reason") or "")
    if halt:
        parts.append(halt)
    night = nights_by_key.get((str(target.get("repo_id") or ""), branch)) if branch else None
    landed = target.get("landed", (night or {}).get("landed"))
    voided = target.get("voided", (night or {}).get("voided"))
    if landed is not None or voided is not None:
        parts.append(f"{_as_int(landed)} landed")
        parts.append(f"{_as_int(voided)} void")
    if state == "error" and target.get("error"):
        parts.append(f"error: {clip(target.get('error'), 120)}")
    return "- " + "  ".join(p for p in parts if p)


def _land_lines(name: str, base: str, branch: str) -> list[str]:
    if base:
        merge = f"- {name}: `git checkout {base} && git merge --no-ff {branch}`"
    else:
        merge = (
            f"- {name}: `git checkout <base> && git merge --no-ff {branch}`"
            "  (default branch unknown: no main, master, or origin/HEAD)"
        )
    return [merge, f"- {name}: `git branch -D {branch}`"]


def render_forum_md(
    forum: dict[str, Any],
    *,
    bag: dict[str, Any] | None = None,
    home: Path | None = None,
) -> str:
    """Human morning read. Not a chat. `## Tonight's bag` only when a bag has a state."""
    if bag is None and home is not None:
        bag = _load_bag_file(home)
    nights = _dict_rows(forum.get("nights"))
    items_by_id = {str(i.get("id") or ""): i for i in _dict_rows(forum.get("items"))}
    nights_by_key = {night_key(n): n for n in nights}
    lines = [
        "# Nightshift forum",
        "Aineko · portfolio ledger · not a chat.",
        f"Updated {forum.get('updated_at') or 'never'}",
        "",
    ]
    if isinstance(bag, dict) and str(bag.get("state") or ""):
        lines.append("## Tonight's bag")
        targets = _dict_rows(bag.get("targets"))
        if targets:
            lines.extend(_bag_target_line(t, nights_by_key) for t in targets)
        else:
            lines.append(f"- (no targets; bag {bag.get('state')})")
        lines.append("")
    lines.append("## Nights")
    if not nights:
        lines.append("- (none yet)")
    for night in sorted(nights, key=_night_sort_key, reverse=True):
        name = str(night.get("repo_name") or night.get("repo_id") or "?")
        branch = str(night.get("branch") or night.get("night") or "")
        halt = str(night.get("halt_reason") or "")
        lines.append(
            f"- {_night_date(night)}  {name}  {branch or '(no branch)'}  {halt}  "
            f"landed {_as_int(night.get('landed'))} / void {_as_int(night.get('voided'))} "
            f"/ open {_as_int(night.get('remaining'))}"
        )
        if night.get("error"):
            lines.append(f"  - error: {clip(night.get('error'), 200)}")
        for item_id_ in _str_list(night.get("item_ids")):
            item = items_by_id.get(item_id_)
            if item is None:
                continue
            title = str(item.get("title") or "(untitled)")
            cmd = str(item.get("check_command") or "")
            line = f"  - {_item_mark(item)} {title}"
            if cmd:
                line += f"  `{cmd}`"
            lines.append(line)
    lines.append("")
    lines.append("## Reuse")
    reuse = _dict_rows(forum.get("reuse_events"))
    if not reuse:
        lines.append("- (none yet)")
    for ev in reuse:
        lines.append(
            f"- {ev.get('kind') or '?'}  {ev.get('origin_repo_name') or ev.get('origin_repo_id') or '?'}"
            f" -> {ev.get('consumer_repo_name') or ev.get('consumer_repo_id') or '?'}"
            f"  {ev.get('consumer_night') or ''}  {ev.get('origin_item_id') or ''}"
        )
    lines.append("")
    lines.append("## Errors")
    errors = _dict_rows(forum.get("errors"))
    if not errors:
        lines.append("- (none)")
    for err in errors:
        lines.append(
            f"- {err.get('at') or ''}  {err.get('repo_name') or err.get('repo_id') or '?'}"
            f"  {clip(err.get('error'), 200)}"
        )
    lines.append("")
    lines.append("## Land")
    land: list[str] = []
    for night in sorted(nights, key=_night_sort_key, reverse=True):
        branch = str(night.get("branch") or "")
        if not branch or night.get("merged") is True:
            continue
        name = str(night.get("repo_name") or night.get("repo_id") or "?")
        land.extend(_land_lines(name, str(night.get("base_ref") or ""), branch))
    lines.extend(land or ["- (none)"])
    return "\n".join(lines) + "\n"
