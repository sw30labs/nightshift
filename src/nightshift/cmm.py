"""CMM: an evidence histogram over the forum. Pure functions, no LLM, no score.

Levels L1-L4 read `forum.json` only (no clone-ledger fallback). L5 adds
`is_nightshift_repo` plus merge evidence: the night row's `merged` flag or
live git evidence through `forum.night_merged` — never a home shard, never
"HEAD is the default branch". A repo is counted once, at its max level. An
empty forum is all L0, including this checkout.

`cmm.json` / `cmm.html` under NIGHTSHIFT_HOME are derived and regenerable.
The page uses the roadmap colour tokens and system fonts only: a morning
without network still renders.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

from .config import Settings
from .forum import atomic_write_json, default_ledger_evidence, night_merged, strict_default_branch
from .ledger import repo_id
from .repos import find_repos
from .safety import is_nightshift_repo, resolve_repo

CMM_SCHEMA = 1
CMM_REL = "cmm.json"
CMM_HTML_REL = "cmm.html"
LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
LEVEL_NAMES = {
    "L0": "unobserved",
    "L1": "checkable DE",
    "L2": "nights with OE",
    "L3": "ledger memory",
    "L4": "forum reuse",
    "L5": "meta RSI",
}
LESSON_PREFIXES = ("duplicate_of_history", "failed_before")
REUSE_KINDS = frozenset({"attempted", "applied"})
MD_BAR_WIDTH = 24
_AINEKO_SVG = (
    '<svg id="aineko" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="#c44928" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true">'
    '<path d="M5 18c-2.8-1.2-3.2-6 1-6.2"/>'
    '<path d="M7 21c0-5 3.4-8 6.4-8h.4V21z"/>'
    '<path d="M13.4 21V13c3.2 0 6 2.4 6 8z"/>'
    '<circle cx="17.4" cy="10" r="3.2"/>'
    '<path d="M14.8 8.4L15.5 5.2l1.7 3"/>'
    '<path d="M17.8 8l1.9-3.4 1 3.6"/>'
    '<circle cx="16.4" cy="10.1" r="0.7" fill="#c44928" stroke="none"/>'
    '<circle cx="18.6" cy="10.1" r="0.7" fill="#c44928" stroke="none"/>'
    "</svg>"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(value: Any) -> list[dict[str, Any]]:
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def _strs(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, (list, tuple)) else []


def _is_work_tree(path: Path) -> bool:
    marker = Path(path) / ".git"
    return marker.is_dir() or marker.is_file()


# --- population --------------------------------------------------------------


def _checkout_at(root: Path) -> Path | None:
    """`root` when it is a git work tree that is Nightshift itself, else None."""
    root = Path(root)
    if _is_work_tree(root) and is_nightshift_repo(root):
        return root
    return None


def package_checkout() -> Path | None:
    """The checkout this package is imported from, when it is one (design note N6).

    Fallback meta locator for population helpers when Nightshift is not
    under roots. Tests patch this to None (N7): a night must never run on
    the operator's checkout from the suite. PR 5 moves it to bag.py.
    """
    return _checkout_at(Path(__file__).resolve().parents[2])


def population(settings: Settings) -> list[Path]:
    """Histogram population: find_repos() plus the meta checkout when it is not under roots."""
    repos = [
        Path(r.path)
        for r in find_repos(settings.roots, include_deprecated=settings.include_deprecated)
    ]
    meta = package_checkout()
    if meta is not None:
        seen = {p.resolve() for p in repos}
        if meta.resolve() not in seen:
            repos.append(meta)
    return repos


# --- scoring -----------------------------------------------------------------


def _night_name(row: dict[str, Any]) -> str:
    return str(row.get("night") or "")


def _is_freeze(night: dict[str, Any]) -> bool:
    """L1: a night with items, or one that halted for any reason but `error` (stubs never count)."""
    if _strs(night.get("item_ids")):
        return True
    return str(night.get("halt_reason") or "") != "error"


def _is_lesson(reason: Any) -> bool:
    return str(reason or "").startswith(LESSON_PREFIXES)


def _lesson_evidence(
    items: list[dict[str, Any]], nights: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """L3: an item, or a night's own void list, with a duplicate_of_history / failed_before reason.

    A freeze that voids a key the ledger already landed never becomes an item
    (the done row is kept), so the night row carries tonight's reasons.
    """
    for item in items:
        if _is_lesson(item.get("void_reason")):
            return {
                "level": 3,
                "kind": "history_void",
                "night": _night_name(item),
                "item_id": str(item.get("id") or ""),
            }
    for night in nights:
        if any(_is_lesson(r) for r in _strs(night.get("void_reasons"))):
            return {"level": 3, "kind": "history_void", "night": _night_name(night)}
    return None


def _meta_evidence(
    path: Path, items: list[dict[str, Any]], nights: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """L5: a done forum item on a Nightshift checkout whose night is merged.

    Merged = the night row says so (git stamp or operator mark-merged), else
    live `forum.night_merged` (default-branch ledger with provenance, or
    merge-base). Git is consulted once per repo, only for unmarked nights.
    """
    by_night: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("done") is True and _night_name(item):
            by_night.setdefault(_night_name(item), []).append(item)
    if not by_night:
        return None
    rows = {_night_name(n): n for n in nights}
    base: str | None = None
    evidence: dict[str, Any] = {}
    for night, done in by_night.items():
        row = rows.get(night)
        merged = row is not None and row.get("merged") is True
        if not merged and _is_work_tree(path):
            if base is None:
                base = strict_default_branch(path)
                evidence = default_ledger_evidence(path, base)
            merged = night_merged(path, night, done, base=base, evidence=evidence)
        if merged:
            return {
                "level": 5,
                "kind": "meta_merged",
                "night": night,
                "item_id": str(done[0].get("id") or ""),
            }
    return None


def score_repo(repo: Path, forum: dict[str, Any], *, home: Path | None = None) -> dict[str, Any]:
    """One repo's level and citations (section 7). Forum rows only for L1-L4.

    L1 a night with a frozen brief; L2 L1 + an item `attempted is True`
    (never last_exit); L3 L1 + a duplicate_of_history / failed_before void
    (L3 does not need L2); L4 a reuse event with this repo as consumer and
    kind attempted / applied (proposed never counts); L5 is_nightshift_repo
    + a done item on a merged night, independent of L4. Level is the max
    satisfied; no forum rows is L0 with empty evidence. `home` is accepted
    for signature parity and never read: home shards are not evidence.
    """
    path = resolve_repo(Path(repo))
    rid = repo_id(path)
    nights = [n for n in _rows(forum.get("nights")) if str(n.get("repo_id") or "") == rid]
    items = [i for i in _rows(forum.get("items")) if str(i.get("repo_id") or "") == rid]
    reuse = [
        e
        for e in _rows(forum.get("reuse_events"))
        if str(e.get("consumer_repo_id") or "") == rid and str(e.get("kind") or "") in REUSE_KINDS
    ]
    evidence: list[dict[str, Any]] = []
    freeze = next((n for n in nights if _is_freeze(n)), None)
    if freeze is not None:
        evidence.append({"level": 1, "kind": "freeze", "night": _night_name(freeze)})
        host = next((i for i in items if i.get("attempted") is True), None)
        if host is not None:
            evidence.append(
                {
                    "level": 2,
                    "kind": "host_check",
                    "night": _night_name(host),
                    "item_id": str(host.get("id") or ""),
                }
            )
        lesson = _lesson_evidence(items, nights)
        if lesson is not None:
            evidence.append(lesson)
    if reuse:
        ev = reuse[0]
        evidence.append(
            {
                "level": 4,
                "kind": "forum_reuse",
                "night": str(ev.get("consumer_night") or ""),
                "event_id": str(ev.get("id") or ""),
            }
        )
    if is_nightshift_repo(path):
        meta = _meta_evidence(path, items, nights)
        if meta is not None:
            evidence.append(meta)
    return {
        "repo_id": rid,
        "repo_name": path.name,
        "repo_path": str(path),
        "level": max((e["level"] for e in evidence), default=0),
        "evidence": evidence,
    }


def histogram(
    repos: list[Path],
    forum: dict[str, Any],
    *,
    home: Path | None = None,
    roots: list[Path] | tuple[Path, ...] = (),
) -> dict[str, Any]:
    """The cmm.json shape: each existing repo once, at its max level. Gone paths are omitted."""
    seen: set[Path] = set()
    scored: list[dict[str, Any]] = []
    for repo in repos:
        path = resolve_repo(Path(repo))
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        scored.append(score_repo(path, forum, home=home))
    counts = {level: 0 for level in LEVELS}
    for row in scored:
        counts[LEVELS[int(row["level"])]] += 1
    return {
        "schema": CMM_SCHEMA,
        "computed_at": _utc_now(),
        "roots": [str(r) for r in roots],
        "histogram": counts,
        "repos": scored,
    }


# --- files -------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_cmm(home: Path, snap: dict[str, Any]) -> Path:
    """Write home/cmm.json (atomic) and home/cmm.html. Returns the JSON path."""
    home_p = Path(home)
    path = home_p / CMM_REL
    atomic_write_json(path, snap)
    _atomic_write_text(home_p / CMM_HTML_REL, render_cmm_html(snap))
    return path


def load_cmm(home: Path) -> dict[str, Any] | None:
    """Tolerant reader for home/cmm.json: None when missing or corrupt."""
    path = Path(home) / CMM_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- render ------------------------------------------------------------------


def _counts(snap: dict[str, Any]) -> dict[str, int]:
    raw = snap.get("histogram") if isinstance(snap.get("histogram"), dict) else {}
    out: dict[str, int] = {}
    for level in LEVELS:
        try:
            out[level] = max(0, int(raw.get(level) or 0))
        except (TypeError, ValueError):
            out[level] = 0
    return out


def _repo_rows(snap: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _rows(snap.get("repos"))

    def key(row: dict[str, Any]) -> tuple[int, str]:
        try:
            level = int(row.get("level") or 0)
        except (TypeError, ValueError):
            level = 0
        return -level, str(row.get("repo_name") or "").lower()

    return sorted(rows, key=key)


def _level_tag(row: dict[str, Any]) -> str:
    try:
        level = int(row.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    return LEVELS[min(max(level, 0), len(LEVELS) - 1)]


def render_cmm_md(snap: dict[str, Any]) -> str:
    """Terminal / morning read: one histogram line per level, then one row per repo."""
    counts = _counts(snap)
    peak = max(counts.values(), default=0)
    name_w = max(len(n) for n in LEVEL_NAMES.values())
    lines = [
        "# Nightshift CMM",
        "Aineko · evidence histogram · not a score.",
        f"Computed {snap.get('computed_at') or 'never'}  repos {sum(counts.values())}",
        "",
    ]
    for level in LEVELS:
        count = counts[level]
        bar = "#" * max(1, round(count * MD_BAR_WIDTH / peak)) if count and peak else ""
        lines.append(f"{level}  {LEVEL_NAMES[level]:<{name_w}}  {bar:<{MD_BAR_WIDTH}}  {count}")
    lines.append("")
    repos = _repo_rows(snap)
    if not repos:
        lines.append("- (no repos)")
    else:
        width = max(len(str(r.get("repo_name") or "?")) for r in repos)
        for row in repos:
            name = str(row.get("repo_name") or "?")
            lines.append(f"{name:<{width}}  {_level_tag(row)}  {row.get('repo_path') or ''}")
    return "\n".join(lines) + "\n"


_HTML = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Nightshift · CMM</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --paper: #e8dfd2; --ink: #14110e; --muted: #6f675e; --soft: #a39a8e;
      --accent: #c44928; --accent-tint: rgba(196,73,40,0.10);
      --sans: system-ui, -apple-system, sans-serif; --serif: ui-serif, Georgia, serif;
      --mono: ui-monospace, Menlo, monospace;
    }
    body { min-height: 100vh; padding: 3rem 2rem; background: var(--paper); color: var(--ink); font-family: var(--sans); }
    .frame { width: 100%; max-width: 1200px; margin: 0 auto; }
    .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
    .eyebrow { margin-bottom: 0.5rem; color: var(--muted); font: 500 0.66rem var(--mono); letter-spacing: 0.18em; text-transform: uppercase; }
    h1 { margin-bottom: 0.4rem; color: var(--ink); font: 400 clamp(1.5rem, 2.4vw + 0.75rem, 2rem)/1.15 var(--serif); letter-spacing: -0.02em; }
    .lede { color: var(--muted); font: 400 14px var(--sans); margin-bottom: 1.5rem; max-width: 42rem; }
    #aineko { width: 40px; height: 40px; flex: 0 0 40px; }
    .chart { overflow-x: auto; }
    svg.diagram { display: block; width: 100%; min-width: 900px; }
    table { width: 100%; margin-top: 1.5rem; border-collapse: collapse; font: 400 12px var(--mono); }
    th { padding: 0.4rem 0.6rem; border-bottom: 1px solid rgba(20,17,14,0.12); color: var(--soft); text-align: left; font-weight: 500; font-size: 8px; letter-spacing: 0.08em; text-transform: uppercase; }
    td { padding: 0.4rem 0.6rem; border-bottom: 1px solid rgba(20,17,14,0.06); vertical-align: top; }
    td.level { color: var(--accent); font-weight: 600; }
    td.num { text-align: right; }
    td.path { color: var(--muted); word-break: break-all; }
    td.empty { color: var(--soft); }
    footer { margin-top: 1.25rem; padding-top: 0.75rem; border-top: 1px solid rgba(20,17,14,0.12); color: var(--soft); font: 400 8px var(--mono); letter-spacing: 0.08em; }
  </style>
</head>
<body>
  <main class="frame">
    <div class="head">
      <div>
        <p class="eyebrow">Nightshift · CMM · local</p>
        <h1>CMM histogram</h1>
      </div>
      $aineko
    </div>
    <p class="lede">Evidence from nights in the forum. Each repo is counted once, at its highest level. A dashed column holds no repo yet. Not a score.</p>
    <div class="chart">
    <svg class="diagram" viewBox="0 0 1040 420" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="cmm-title cmm-desc">
      <title id="cmm-title">CMM histogram</title>
      <desc id="cmm-desc">$desc</desc>
      <rect width="1040" height="420" fill="#e8dfd2"/>
      <line x1="56" y1="48" x2="56" y2="280" stroke="#a39a8e" stroke-width="1"/>
      <line x1="56" y1="280" x2="1000" y2="280" stroke="#a39a8e" stroke-width="1"/>
      <text x="40" y="44" fill="#a39a8e" font-size="8" font-family="ui-monospace, monospace" letter-spacing="0.08em">REPOS</text>
$columns
      <line x1="30" y1="356" x2="1010" y2="356" stroke="rgba(20,17,14,0.10)" stroke-width="0.8"/>
      <text x="56" y="380" fill="#a39a8e" font-size="8" font-family="ui-monospace, monospace" letter-spacing="0.14em">LEGEND</text>
      <rect x="160" y="368" width="16" height="12" rx="2" fill="#e8dfd2" stroke="#a39a8e" stroke-dasharray="4 3"/>
      <text x="184" y="378" fill="#6f675e" font-size="8" font-family="ui-monospace, monospace">empty · no repo at this level</text>
      <rect x="400" y="368" width="16" height="12" rx="2" fill="rgba(196,73,40,0.10)" stroke="#c44928"/>
      <text x="424" y="378" fill="#6f675e" font-size="8" font-family="ui-monospace, monospace">repos · height is count</text>
    </svg>
    </div>
    <table>
      <thead><tr><th>repo</th><th>level</th><th>evidence</th><th>path</th></tr></thead>
      <tbody>
$rows
      </tbody>
    </table>
    <footer>EVIDENCE FROM NIGHTS · NOT A SCORE · COMPUTED $computed_at · $population REPOS</footer>
  </main>
</body>
</html>
"""
)

_COL_X0 = 96
_COL_STEP = 152
_COL_W = 96
_BASE_Y = 280
_MAX_H = 232
_MIN_H = 12


def _column_svg(index: int, level: str, count: int, peak: int) -> str:
    x = _COL_X0 + _COL_STEP * index
    cx = x + _COL_W // 2
    if count > 0 and peak > 0:
        h = max(_MIN_H, round(_MAX_H * count / peak))
        style = 'fill="rgba(196,73,40,0.10)" stroke="#c44928" stroke-width="1.2"'
        count_fill = "#14110e"
    else:
        h = _MIN_H
        style = 'fill="#e8dfd2" stroke="#a39a8e" stroke-width="1" stroke-dasharray="4 3"'
        count_fill = "#a39a8e"
    y = _BASE_Y - h
    label_fill = "#c44928" if level == "L5" else "#14110e"
    return "\n".join(
        [
            f'      <rect data-level="{level}" x="{x}" y="{y}" width="{_COL_W}" height="{h}" rx="4" {style}/>',
            f'      <text data-count="{level}" x="{cx}" y="{y - 8}" text-anchor="middle" fill="{count_fill}" '
            f'font-size="12" font-weight="600" font-family="system-ui, sans-serif">{count}</text>',
            f'      <text x="{cx}" y="304" text-anchor="middle" fill="{label_fill}" font-size="12" '
            f'font-weight="600" font-family="system-ui, sans-serif">{level}</text>',
            f'      <text x="{cx}" y="320" text-anchor="middle" fill="#a39a8e" font-size="8" '
            f'font-family="ui-monospace, monospace">{html.escape(LEVEL_NAMES[level])}</text>',
        ]
    )


def _table_row(row: dict[str, Any]) -> str:
    name = html.escape(str(row.get("repo_name") or "?"))
    path = html.escape(str(row.get("repo_path") or ""))
    evidence = len(_rows(row.get("evidence")))
    return (
        f'        <tr><td>{name}</td><td class="level">{_level_tag(row)}</td>'
        f'<td class="num">{evidence}</td><td class="path">{path}</td></tr>'
    )


def render_cmm_html(snap: dict[str, Any]) -> str:
    """Local page: roadmap colours, Aineko far right, system fonts, no external resources."""
    counts = _counts(snap)
    peak = max(counts.values(), default=0)
    columns = "\n".join(
        _column_svg(i, level, counts[level], peak) for i, level in enumerate(LEVELS)
    )
    repos = _repo_rows(snap)
    rows = "\n".join(_table_row(r) for r in repos) or (
        '        <tr><td class="empty" colspan="4">(no repos)</td></tr>'
    )
    desc = "Six levels from unobserved to meta RSI. " + ", ".join(
        f"{level} {LEVEL_NAMES[level]}: {counts[level]}" for level in LEVELS
    )
    return _HTML.substitute(
        aineko=_AINEKO_SVG,
        desc=html.escape(desc),
        columns=columns,
        rows=rows,
        computed_at=html.escape(str(snap.get("computed_at") or "never")),
        population=sum(counts.values()),
    )
