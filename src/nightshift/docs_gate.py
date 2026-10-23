"""ADR + assumptions gate (ADR 0007). Job 1 of every night when docs are missing.

Stdlib only and runnable as a script, because the frozen check command is
`python <this file> --check` under the *target* repo's interpreter:

    python /path/to/nightshift/docs_gate.py --check   # cwd = target repo

Audit rules (deterministic, no model):
- Gap (fires the gate): no ADR folder under docs/ (adr, adrs or decisions) with
  at least one ADR file, or no assumptions register (a markdown file under
  docs/ whose name contains "assumption") with at least one table row or list
  item. Hand-written ADRs in another house style are never a gap.
- With --base SHA, ADR files added since SHA must also carry a status line,
  Context, Decision and Evidence sections, and a link from the folder's
  README.md index. That is how the night proves the new ADRs are complete.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ADR_DIR_NAMES = ("adr", "adrs", "decisions")
DEFAULT_ADR_DIR = "docs/adr"
DEFAULT_ASSUMPTIONS = "docs/assumptions.md"
INDEX_NAMES = ("README.md", "readme.md", "index.md")
NOT_ADR = ("readme", "index", "template")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
MAX_DEPTH = 4

_STATUS = re.compile(
    r"\bstatus\b|\b(accepted|proposed|superseded|deprecated|rejected)\b", re.I
)
_CONTEXT = re.compile(r"^#{1,6}\s*(\d+[.)]?\s*)?context\b", re.I | re.M)
_DECISION = re.compile(r"^#{1,6}\s*(\d+[.)]?\s*)?decision\b", re.I | re.M)
_EVIDENCE = re.compile(r"^#{1,6}\s*(\d+[.)]?\s*)?(evidence|references|sources)\b", re.I | re.M)
_LOOSE_ADR = re.compile(r"^adr[-_ ]?\d+.*\.md$", re.I)
_TABLE_ROW = re.compile(r"^\s*\|(?!\s*:?-{3,}).*\|\s*$", re.M)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.M)


@dataclass
class DocsAudit:
    adr_dir: str = ""
    adrs: list[str] = field(default_factory=list)
    assumptions: str = ""
    problems: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _walk_docs(repo: Path):
    docs = repo / "docs"
    if not docs.is_dir() or docs.is_symlink():
        return
    stack = [(docs, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            kids = sorted(cur.iterdir())
        except OSError:
            continue
        for kid in kids:
            if kid.is_symlink() or kid.name in SKIP_DIRS:
                continue
            yield kid
            if kid.is_dir() and depth + 1 < MAX_DEPTH:
                stack.append((kid, depth + 1))


def _rel(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def _adr_files(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.glob("*.md")):
        stem = p.stem.lower()
        if any(stem.startswith(word) for word in NOT_ADR):
            continue
        out.append(p)
    return out


def find_adr_dir(repo: Path) -> Path | None:
    """The docs/ folder holding the most ADR files; None when there is none."""
    best: tuple[int, int, str] | None = None
    pick: Path | None = None
    for path in _walk_docs(repo):
        if not path.is_dir() or path.name.lower() not in ADR_DIR_NAMES:
            continue
        rel = _rel(repo, path)
        key = (len(_adr_files(path)), -rel.count("/"), rel)
        if best is None or key > best:
            best, pick = key, path
    return pick


def find_assumptions(repo: Path) -> Path | None:
    hits = [
        p
        for p in _walk_docs(repo)
        if p.is_file() and p.suffix.lower() == ".md" and "assumption" in p.name.lower()
    ]
    hits.sort(key=lambda p: (_rel(repo, p).count("/"), _rel(repo, p)))
    return hits[0] if hits else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def count_entries(text: str) -> int:
    rows = _TABLE_ROW.findall(text)
    table = max(len(rows) - 1, 0)  # first row is the header
    return table + len(_LIST_ITEM.findall(text))


def added_since(repo: Path, base: str) -> set[str]:
    """Paths added since base: committed on the night branch or still untracked."""
    out: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", "--diff-filter=A", base, "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ):
        try:
            proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
        except OSError:
            continue
        out.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return out


def _adr_shape(text: str, *, new: bool) -> list[str]:
    missing = []
    if not _STATUS.search("\n".join(text.splitlines()[:15])):
        missing.append("status")
    if not _CONTEXT.search(text):
        missing.append("Context")
    if not _DECISION.search(text):
        missing.append("Decision")
    if new and not _EVIDENCE.search(text):
        missing.append("Evidence")
    return missing


def audit(repo: Path, *, base: str = "") -> DocsAudit:
    repo = Path(repo)
    result = DocsAudit()
    added = added_since(repo, base) if base else set()
    folder = find_adr_dir(repo)
    if folder is None:
        result.problems.append("no ADR folder under docs/ (docs/adr, docs/adrs or docs/decisions)")
        loose = [
            _rel(repo, p)
            for p in _walk_docs(repo)
            if p.is_file() and _LOOSE_ADR.match(p.name)
        ]
        if loose:
            result.advisories.append(
                "ADR files outside an ADR folder (link them from the new index; "
                "do not move or rewrite them): " + ", ".join(loose[:10])
            )
    else:
        result.adr_dir = _rel(repo, folder)
        files = _adr_files(folder)
        result.adrs = [_rel(repo, p) for p in files]
        if not files:
            result.problems.append(f"{result.adr_dir}/ holds no ADR files")
        index = next((folder / n for n in INDEX_NAMES if (folder / n).is_file()), None)
        index_text = _read(index) if index is not None else ""
        for p in files:
            rel = _rel(repo, p)
            new = rel in added
            missing = _adr_shape(_read(p), new=new)
            linked = index is not None and p.name in index_text
            if new:
                if missing:
                    result.problems.append(f"new {rel} lacks {', '.join(missing)}")
                if not linked:
                    result.problems.append(f"new {rel} is not linked from {result.adr_dir}/README.md")
            else:
                if missing:
                    result.advisories.append(f"{rel} has no {', '.join(missing)} heading")
                if index is not None and not linked:
                    result.advisories.append(f"{rel} is not linked from the ADR index")
        if files and index is None:
            result.advisories.append(f"{result.adr_dir}/ has no README.md index")
    register = find_assumptions(repo)
    if register is None:
        result.problems.append("no assumptions register under docs/ (e.g. docs/assumptions.md)")
    else:
        result.assumptions = _rel(repo, register)
        if count_entries(_read(register)) < 1:
            result.problems.append(f"{result.assumptions} has no entries")
    return result


GATE_TITLE = "ADR + assumptions gate: document the project's decisions and assumptions"
GATE_MARKER = "docs_gate.py"


def check_command(base_sha: str = "") -> str:
    cmd = f"python {shlex.quote(str(Path(__file__).resolve()))} --check"
    if base_sha:
        cmd += f" --base {shlex.quote(base_sha)}"
    return cmd


def is_gate(upgrade) -> bool:
    return GATE_MARKER in str(getattr(upgrade, "check_command", "") or "")


def gate_note(result: DocsAudit, adr_dir: str, register: str) -> str:
    style = (
        f"Match the format already used in {adr_dir}/."
        if result.adrs
        else (
            "Format (Porter Forces style): NNN-kebab-title.md (three digits); "
            "'# ADR NNN: Title', then '- Status: Accepted (retrospective)', "
            "'- Date: YYYY-MM-DD' (first commit that shows the decision), "
            "'- Decision owners: ...'; sections Context, Decision, "
            "Alternatives considered, Consequences, Evidence."
        )
    )
    return (
        "Non-negotiable, runs first. Read the COMPLETE project in the snapshot "
        "(code, tests, config, README, docs, git log) before writing. "
        f"Write retrospective ADRs in {adr_dir}/: one file per real, load-bearing "
        "decision visible in code, config, commits or existing docs. Never invent a "
        "decision. Any rationale not stated in code, docs or commits is labelled "
        "'(inferred)'. Each ADR cites its evidence (file paths, commit hashes). "
        f"{style} Keep {adr_dir}/README.md as an index table linking every ADR. "
        f"Write {register} as an assumptions register: a table with ID, Assumption, "
        "Evidence, Stated/Inferred, Impact if wrong, Review owner. "
        f"Gaps at freeze: {'; '.join(result.problems)}"
        + (f". Existing-ADR notes (fix only if cheap): {'; '.join(result.advisories[:5])}" if result.advisories else "")
    )


def gate_upgrade(repo: Path, base_sha: str = ""):
    """The job-1 Upgrade when the gate is not met; None when docs are in place."""
    from .models import Upgrade

    result = audit(repo)
    if result.ok:
        return None
    adr_dir = result.adr_dir or DEFAULT_ADR_DIR
    register = result.assumptions or DEFAULT_ASSUMPTIONS
    return Upgrade(
        id=1,
        title=GATE_TITLE,
        check_command=check_command(base_sha),
        paths=[adr_dir.rstrip("/") + "/", register],
        note=gate_note(result, adr_dir, register),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo = Path.cwd()
    base = ""
    i = 0
    while i < len(args):
        if args[i] == "--base" and i + 1 < len(args):
            base = args[i + 1]
            i += 2
            continue
        if not args[i].startswith("-"):
            repo = Path(args[i])
        i += 1
    result = audit(repo, base=base)
    if result.ok:
        print(
            f"docs gate OK: {len(result.adrs)} ADRs in {result.adr_dir}/, "
            f"assumptions register {result.assumptions}"
        )
        return 0
    print("docs gate FAILED:")
    for problem in result.problems:
        print(f"- {problem}")
    for note in result.advisories:
        print(f"  (note) {note}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
