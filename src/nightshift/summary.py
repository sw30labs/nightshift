"""Morning artifacts: summary.md, `nightshift morning`, land commands."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .gitops import commits_since, commits_touching, default_branch, diff_stat_against, git, rev_parse
from .graph import load_turns
from .llm import persist_meta
from .models import Brief

_NIGHT_DATE = re.compile(r"night/(\d{4}-\d{2}-\d{2})")


def date_from_branch(branch: str, fallback: str) -> str:
    m = _NIGHT_DATE.search(branch or "")
    return m.group(1) if m else fallback


def halt_words(halt_reason: str, *, remaining: int, turn: int, job: str = "") -> str:
    reason = halt_reason or "unknown"
    mapping = {
        "remaining_zero": "brief exhausted",
        "clock": f"06:00 halt, {remaining} jobs still open",
        "max_turns": f"{turn} turns spent",
        "stalled": f"loop stalled after {turn} turns without progress",
        "requested": f"halted on request after turn {turn}",
        "critic": "critic halted",
        "error": f"crashed at turn {turn}",
    }
    return mapping.get(reason, reason)


def night_view(repo: Path, *, branch: str | None = None) -> dict[str, Any]:
    repo = Path(repo)
    brief_path = repo / ".nightshift" / "brief.json"
    if not brief_path.is_file():
        raise FileNotFoundError("no night")
    data = json.loads(brief_path.read_text(encoding="utf-8"))
    brief = Brief.from_dict(data)
    branch = branch or str(data.get("branch") or "")
    base_ref = str(data.get("base_ref") or default_branch(repo))
    base_sha = str(data.get("base_sha") or "")
    if not base_sha:
        try:
            base_sha = rev_parse(repo, base_ref)
        except Exception:
            base_sha = ""
    main_ref = default_branch(repo)
    try:
        main_sha = rev_parse(repo, main_ref)
    except Exception:
        main_sha = ""
    turns = load_turns(repo)
    jobs: list[dict[str, Any]] = []
    for upgrade in brief.upgrades:
        if upgrade.void:
            state = "void"
        elif upgrade.done:
            state = "done"
        else:
            state = "open"
        job_turns = [t for t in turns if int(t.get("upgrade_id") or 0) == upgrade.id]
        last = job_turns[-1] if job_turns else {}
        check = last.get("check") or {}
        commits = commits_touching(repo, base_sha, list(upgrade.paths)) if base_sha else []
        jobs.append(
            {
                "id": upgrade.id,
                "title": upgrade.title,
                "state": state,
                "void_reason": upgrade.void_reason,
                "note": upgrade.note,
                "check": upgrade.check_command,
                "paths": list(upgrade.paths),
                "turns": len(job_turns),
                "final_exit": check.get("exit_code"),
                "fingerprint": check.get("fingerprint") or "",
                "tail": check.get("tail") or "",
                "commits": commits,
            }
        )
    landed = sum(1 for j in jobs if j["state"] == "done")
    voided = sum(1 for j in jobs if j["state"] == "void")
    open_n = sum(1 for j in jobs if j["state"] == "open")
    halt_reason = ""
    error = ""
    summary_path = repo / ".nightshift" / "summary.md"
    if summary_path.is_file():
        text = summary_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\*\*Halt:\*\* (\S+)", text)
        if m:
            halt_reason = m.group(1)
        if "crashed" in text.lower():
            error = text
    keepers = [
        sha
        for j in jobs
        if j["state"] == "done"
        for sha in j["commits"]
    ]
    log = commits_since(repo, base_sha) if base_sha else ""
    commit_rows = []
    for line in (log or "").splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        commit_rows.append(
            {
                "sha": sha,
                "subject": subject,
                "keeper": sha in keepers or any(sha.startswith(k) for k in keepers),
            }
        )
    stat = ""
    if base_sha:
        stat = diff_stat_against(
            repo, base_sha, extra=["--", ".", ":!.nightshift"]
        )
    refused_by_turn: dict[int, list[str]] = {}
    for row in turns:
        notes = list(row.get("writer_refused") or []) + list(row.get("critic_notes") or [])
        if notes:
            refused_by_turn[int(row.get("turn") or 0)] = notes
    view = {
        "verdict": f"{landed} of {len(jobs)} landed · {voided} void · {open_n} open",
        "branch": branch,
        "base": {"ref": base_ref, "sha": base_sha},
        "main": {"ref": main_ref, "sha": main_sha},
        "halt_reason": halt_reason,
        "halt_words": halt_words(
            halt_reason, remaining=open_n, turn=max((t.get("turn") or 0) for t in turns) if turns else 0
        ),
        "error": error,
        "jobs": jobs,
        "commits": commit_rows,
        "changed_stat": stat,
        "review_cmd": f"git diff {base_sha[:7] or base_ref}...{branch} -- . ':!.nightshift'"
        if branch
        else "",
        "land": {
            "merge": f"git checkout {base_ref} && git merge --no-ff {branch}",
            "cherry_pick": ("git cherry-pick " + " ".join(keepers)) if keepers else "",
            "drop": f"git branch -D {branch}",
        },
        "refused_by_turn": refused_by_turn,
        "remaining": open_n,
        "landed": landed,
        "voided": voided,
    }
    return view


def render_markdown(
    view: dict[str, Any],
    *,
    date: str,
    repo: Path,
    extra_header: list[str] | None = None,
    refused_fallback: list[str] | None = None,
) -> str:
    jobs = list(view.get("jobs") or [])
    landed = int(view.get("landed") or 0)
    voided = int(view.get("voided") or 0)
    remaining = int(view.get("remaining") or 0)
    halt_reason = str(view.get("halt_reason") or "")
    words = str(view.get("halt_words") or halt_reason)
    body: list[str] = [
        f"# Nightshift — {date}",
        "",
        f"**{landed} of {len(jobs)} landed · {voided} void · {remaining} open — {words}**",
        "",
        f"**Repo:** `{repo}`",
        f"**Branch:** `{view.get('branch') or ''}`",
        f"**Halt:** {halt_reason or 'unknown'}",
        f"**Remaining:** {remaining}",
    ]
    base = view.get("base") or {}
    main = view.get("main") or {}
    if base.get("ref"):
        sha7 = str(base.get("sha") or "")[:7]
        body.append(f"**Base:** {base.get('ref')} @ {sha7}")
    if main.get("ref"):
        sha7 = str(main.get("sha") or "")[:7]
        body.append(f"**Main:** {main.get('ref')} @ {sha7} (unchanged)")
    if extra_header:
        body.extend(extra_header)
    body += ["", "## Frozen brief", ""]
    for job in jobs:
        mark = job.get("state") or "open"
        body.append(f"- **#{job['id']} [{mark}]** {job.get('title')}")
        body.append(f"  - check: `{job.get('check')}`")
        paths = job.get("paths") or []
        body.append(f"  - paths: {', '.join(paths) or '(none)'}")
        if job.get("note"):
            body.append(f"  - note: {job['note']}")
        if job.get("turns"):
            body.append(f"  - turns: {job['turns']}")
        if job.get("commits"):
            body.append(f"  - commits: {', '.join(job['commits'])}")
        if mark in {"open", "void"} and job.get("fingerprint"):
            body.append(f"  - fingerprint: `{job['fingerprint']}`")
            tail = str(job.get("tail") or "")
            if tail:
                last_lines = "\n".join(tail.splitlines()[-15:])
                body.append("  ```")
                body.append(last_lines)
                body.append("  ```")
    body += ["", "## Voided / skipped-as-duplicate", ""]
    void_jobs = [j for j in jobs if j.get("state") == "void"]
    if void_jobs:
        for job in void_jobs:
            reason = job.get("void_reason") or "void"
            body.append(f"- **#{job['id']} [{reason}]** {job.get('title')}")
            body.append(f"  - check: `{job.get('check')}`")
    else:
        body.append("- none")
    body += ["", "## What changed", ""]
    body.append("```")
    commits = view.get("commits") or []
    if commits:
        for row in commits:
            body.append(f"{row.get('sha')} {row.get('subject')}")
    else:
        body.append("(no commits)")
    body.append("```")
    body.append("")
    body.append("```")
    body.append(str(view.get("changed_stat") or "(no diff)"))
    body.append("```")
    if view.get("review_cmd"):
        body.append("")
        body.append(f"`{view['review_cmd']}`")
    land = view.get("land") or {}
    body += ["", "## Land it", ""]
    if land.get("merge"):
        body.append(f"- merge: `{land['merge']}`")
    if land.get("cherry_pick"):
        body.append(f"- cherry-pick keepers: `{land['cherry_pick']}`")
    if land.get("drop"):
        body.append(f"- drop: `{land['drop']}`")
    body.append("- `.nightshift/ledger.json` rides along on merge")
    body += ["", "## What the critic refused", ""]
    refused_by_turn = view.get("refused_by_turn") or {}
    if refused_by_turn:
        for turn in sorted(refused_by_turn):
            notes = list(refused_by_turn[turn])
            seen: set[str] = set()
            uniq: list[str] = []
            for n in notes:
                if n in seen:
                    continue
                seen.add(n)
                uniq.append(n)
            extra = ""
            if len(uniq) > 6:
                extra = f" (+{len(uniq) - 6} similar)"
                uniq = uniq[:6]
            body.append(f"- turn {turn}{extra}")
            for n in uniq:
                body.append(f"  - {n}")
    elif refused_fallback:
        for note in refused_fallback:
            body.append(f"- {note}")
    else:
        body.append("- nothing slashed this night")
    if remaining:
        body += ["", "## Remaining", ""]
        for job in jobs:
            if job.get("state") == "open":
                body.append(
                    f"- **#{job['id']}** {job.get('title')} — `{job.get('check')}`"
                )
        body.append("")
    return "\n".join(body) + "\n"


def render_terminal(view: dict[str, Any], width: int = 80) -> str:
    lines = [
        str(view.get("verdict") or ""),
        f"branch  {view.get('branch') or ''}",
        f"halt    {view.get('halt_reason') or ''} — {view.get('halt_words') or ''}",
        "",
    ]
    for job in view.get("jobs") or []:
        title = str(job.get("title") or "")
        if width and len(title) > max(20, width - 28):
            title = title[: max(17, width - 31)] + "..."
        lines.append(
            f"  #{job.get('id')}  {job.get('state'):4}  t={job.get('turns') or 0}  "
            f"exit={job.get('final_exit')}  {title}"
        )
    land = view.get("land") or {}
    lines += ["", "Land it:"]
    for key in ("merge", "cherry_pick", "drop"):
        if land.get(key):
            lines.append(f"  {land[key]}")
    for job in view.get("jobs") or []:
        if job.get("state") in {"open", "void"} and job.get("tail"):
            lines.append(f"\n#{job['id']} tail:")
            for ln in str(job["tail"]).splitlines()[-8:]:
                lines.append(f"    {ln}")
    return "\n".join(lines) + "\n"


def write_summary_file(repo: Path, text: str) -> Path:
    persist_meta(repo, ".nightshift/summary.md", text)
    return repo / ".nightshift" / "summary.md"
