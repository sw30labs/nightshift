"""Owner portfolio decisions → bounded bags. Porter pressure is not a rank.

Only the public assessment is fetched. Private TwinOps context stays outside
public repo artifacts. No cloud model or page-rendering dependency is needed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .forum import atomic_write_json
from .models import SafetyError

SOURCE = "https://raw.githubusercontent.com/sw30labs/.github/main/docs/porter-data.json"
PAGE = "https://sw30labs.github.io/.github/porter.html"


def policy(home: Path) -> dict | None:
    path = home / "portfolio.json"
    if not path.exists():
        return None  # Existing installations opt in explicitly.
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("source") != SOURCE:
            raise ValueError("source must be the owner-controlled SW30 assessment")
        if not isinstance(data.get("paused", False), bool):
            raise ValueError("paused must be boolean")
        if not isinstance(data.get("repo_map", {}), dict) or not isinstance(data.get("approved_assessments", {}), dict):
            raise ValueError("repo_map and approved_assessments must be objects")
        return data
    except (OSError, ValueError) as exc:
        raise SafetyError(f"invalid portfolio.json: {exc}") from exc


def fetch_source() -> bytes:
    request = Request(SOURCE, headers={"User-Agent": "Nightshift-portfolio/1", "Cache-Control": "no-cache"})
    with urlopen(request, timeout=20) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("assessment exceeds 1 MB")
    return raw


def refresh(home: Path, config: dict) -> dict:
    """Fetch each selection; archive by digest, never fall back to stale data."""
    try:
        raw = fetch_source()
        data = json.loads(raw)
        assessed = date.fromisoformat(data["date"])
        if assessed > datetime.now(timezone.utc).date():
            raise ValueError("assessment date is in the future")
        projects = data["projects"]
        if not isinstance(projects, list) or not projects:
            raise ValueError("projects must be a nonempty list")
        seen = set()
        total = 0
        for row in projects:
            ident = row["id"]
            if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", ident) or ident in seen:
                raise ValueError("invalid or duplicate project id")
            seen.add(ident)
            weight = row["allocation"]
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or not 0 <= weight <= 100:
                raise ValueError("allocation must be finite, 0–100")
            total += weight
            if row["group"] not in {"Focus", "Maintain", "Rotate", "Gate", "Frontier", "Park"}:
                raise ValueError("unknown priority group")
            for key in ("night", "human", "gate", "amend"):
                if not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > 8000:
                    raise ValueError(f"missing or oversized {key}")
        if total > 100:
            raise ValueError("allocations exceed 100 percent")
        digest = hashlib.sha256(raw).hexdigest()
        snapshot = {"source": SOURCE, "page": PAGE, "sha256": digest,
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "assessment": data}
        atomic_write_json(home / "priority-snapshots" / f"{digest}.json", snapshot)
        atomic_write_json(home / "priority-latest.json", snapshot)
        return snapshot
    except Exception as exc:
        atomic_write_json(home / "priority-error.json", {
            "at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc), "decision": "No bag: current priorities unavailable or invalid; no recency fallback.",
        })
        raise SafetyError(f"portfolio refresh failed; no bag started: {exc}") from exc


def remote_slug(path: Path) -> str:
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
    remote = r.stdout.strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", remote)
    return match.group(1).lower() if match else ""


def row_slug(row: dict, config: dict) -> str:
    # RPC's public dossier intentionally does not link its private repository.
    override = config.get("repo_map", {}).get(row["id"])
    if override:
        slug = override
    else:
        match = re.fullmatch(r"https://github\.com/(sw30labs/[^/#]+?)(?:\.git)?/?", row.get("repo", ""))
        slug = match.group(1) if match else ""
    if slug and not re.fullmatch(r"sw30labs/[A-Za-z0-9_.-]+", slug):
        raise SafetyError("repo_map must identify an SW30 repository")
    return slug.lower()


def choose(candidates: list, snapshot: dict, config: dict, home: Path, *, size: int) -> tuple[list, list, list]:
    """Weighted service by attempted nights, not by commit recency or scores.

    Shares are relative scheduling weights, not measured hours. Zero/Gate/Park
    never auto-run. Frontier requires a digest-bound owner approval.
    """
    history_path = home / "priority-service.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {}
    pending = [x for x in history.values() if x.get("review") == "pending"]
    limit = config.get("max_pending_reviews", 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SafetyError("max_pending_reviews must be a positive integer")
    if len(pending) >= limit:
        raise SafetyError("Morning Prayers review pending; record a HOTL disposition before another bag")
    rows = snapshot["assessment"]["projects"]
    by_remote = {}
    for target in candidates:
        by_remote.setdefault(remote_slug(target.path), []).append(target)
    selected = []
    excluded = []
    diagnostics = []
    for row in rows:
        ident = row["id"]
        reason = ""
        if row["group"] in {"Gate", "Park"} or row["allocation"] <= 0:
            reason = "gated or zero allocation"
        elif row["group"] == "Frontier" and config.get("approved_assessments", {}).get(ident) != snapshot["sha256"]:
            reason = "Frontier assumptions need owner approval for this assessment digest"
        slug = row_slug(row, config)
        matches = by_remote.get(slug, []) if slug else []
        if not reason and len(matches) != 1:
            reason = "no unique eligible local checkout for repository"
        if reason:
            diagnostics.append({"project": ident, "reason": reason})
            continue
        target = matches[0]
        count = sum(1 for x in history.values() if x.get("project") == ident)
        target.priority = {"id": ident, "group": row["group"], "allocation": row["allocation"],
                           "night": row["night"], "human": row["human"], "gate": row["gate"],
                           "amend": row["amend"], "source": SOURCE, "sha256": snapshot["sha256"],
                           "assessment_date": snapshot["assessment"]["date"],
                           "selection_reason": "lowest attempted-nights / allocation; highest allocation breaks ties"}
        selected.append((count / row["allocation"], -row["allocation"], ident, target))
    selected.sort(key=lambda x: x[:3])
    chosen = [x[3] for x in selected[:size]]
    for target in candidates:
        if target not in chosen:
            target.skip_reason = "not selected by portfolio priority; see priority diagnostics"
            excluded.append(target)
    return chosen, excluded, diagnostics


def record_attempt(home: Path, bag_id: str, target) -> None:
    if not target.priority:
        return
    from .forum import with_home_lock
    def update():
        path = home / "priority-service.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        key = f"{bag_id}:{target.priority['id']}"
        data.setdefault(key, {"project": target.priority["id"], "bag_id": bag_id,
                              "sha256": target.priority["sha256"], "review": "pending",
                              "at": datetime.now(timezone.utc).isoformat()})
        atomic_write_json(path, data)
    with_home_lock(home, "priority-service", update)


def context(priority: dict) -> str:
    if not priority:
        return ""
    return ("## Owner portfolio priority (scope for this night)\n"
            + json.dumps(priority, ensure_ascii=False, indent=2)
            + "\nChoose only small host-checkable work advancing the night objective above. "
            "Do not substitute generic cleanup, add scope, change allocations, or execute the human/gate work. "
            "If prerequisite decisions are missing, halt with a concrete blocker. "
            "Porter pressure scores are market context, not execution rankings. "
            "TwinOps is background memory, not authority to override current owner priorities.\n")


def morning(home: Path) -> str:
    from .bag import load_bag
    bag = load_bag(home)
    p = bag.get("priorities")
    error = home / "priority-error.json"
    failure = json.loads(error.read_text()) if error.exists() else {}
    if failure and (not p or failure.get("at", "") > p.get("fetched_at", "")):
        return "## Portfolio priority refresh failed (latest attempt)\n\n" + json.dumps(failure, indent=2) + "\nPrevious bag is historical; no new priority-driven run was started.\n"
    if not p:
        return ""
    lines = ["## Portfolio → Nightshift → Morning Prayers", "",
             f"Bag: {bag.get('bag_id')} · state: {bag.get('state')}",
             f"Priority source: {p['source']}", f"Snapshot: {p['sha256']} · fetched: {p['fetched_at']}", ""]
    for row in bag.get("targets", []):
        priority = row.get("priority", {})
        lines += [f"### {row['name']} — {row.get('state')}",
                  f"Objective: {priority.get('night', '')}", f"Gate: {priority.get('gate', '')}",
                  f"Human decision: {priority.get('human', '')}",
                  f"Branch: {row.get('branch') or '(none)'} · verified jobs: {row.get('landed')} · remaining: {row.get('remaining_count')}",
                  "HOTL disposition: accept / reject / defer. Record actual review/repair minutes; passing jobs are not merged work.", ""]
    for row in p.get("diagnostics", []):
        lines.append(f"- Skipped {row['project']}: {row['reason']}")
    for row in bag.get("skipped", []):
        lines.append(f"- Checkout skipped {row['name']}: {row.get('error', '')}")
    return "\n".join(lines) + "\n"


def main():
    """Record HOTL disposition; never merge, push or change project priorities."""
    import argparse
    from .config import Settings
    from .forum import with_home_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["review"])
    parser.add_argument("bag_id")
    parser.add_argument("project")
    parser.add_argument("decision", choices=["accept", "reject", "defer"])
    parser.add_argument("--minutes", type=float, required=True)
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    if not math.isfinite(args.minutes) or args.minutes < 0 or not args.note.strip():
        parser.error("finite nonnegative minutes and a review note are required")
    home = Settings().home
    def update():
        path = home / "priority-service.json"
        data = json.loads(path.read_text())
        key = f"{args.bag_id}:{args.project}"
        if key not in data:
            raise SafetyError("unknown bag/project; no review recorded")
        row = data[key]
        row.setdefault("reviews", []).append({"decision": args.decision, "minutes": args.minutes,
                                               "note": args.note, "at": datetime.now(timezone.utc).isoformat()})
        row["review"] = "pending" if args.decision == "defer" else args.decision
        atomic_write_json(path, data)
    with_home_lock(home, "priority-service", update)
    print("HOTL disposition recorded; no code merged or pushed.")


if __name__ == "__main__":
    main()
