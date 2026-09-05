"""OpenAI-compatible HTTP clients, mock provider, writer tools, critic (no write)."""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import BRIEF_SIZE_DEFAULT, Brief, FrozenBriefError, SafetyError, Upgrade, WriterResult
from .safety import assert_inside_repo, assert_job_path, is_meta_path

MAX_FULL_FILE_CHARS = 8_000
WRITER_SNAPSHOT_CHARS = int(os.environ.get("NIGHTSHIFT_WRITER_SNAPSHOT_CHARS", "120000"))


class ChatClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> str: ...


def parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    blobs = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        blobs.append(raw[start : end + 1])
    seen: set[str] = set()
    for blob in blobs:
        if blob in seen:
            continue
        seen.add(blob)
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in model output")


def completion_text(body: dict[str, Any]) -> str:
    """Prefer message.content; GLM 5.3 / oMLX often fills reasoning_content instead."""
    try:
        msg = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected chat completion shape: {body!r}") from exc
    if not isinstance(msg, dict):
        raise RuntimeError(f"unexpected chat completion shape: {body!r}")
    for key in ("content", "reasoning_content", "reasoning"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _rows(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return raw
    return []


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x) for x in raw if x is not None and str(x).strip()]
    return [str(raw)]


def _as_id_list(raw: Any) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = [raw]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for x in raw:
        if x is None or isinstance(x, bool):
            continue
        if isinstance(x, int):
            out.append(x)
            continue
        if isinstance(x, float):
            if math.isfinite(x) and x.is_integer():
                out.append(int(x))
            continue
        s = str(x).strip()
        m = re.search(r"\d+", s)
        if m:
            out.append(int(m.group(0)))
    return out


class OpenAICompatClient:
    """POST {base}/chat/completions. oMLX expects Bearer test; Spark vLLM ignores it."""

    mock = False

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "test",
        timeout: float = 600,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or "test"
        self.timeout = timeout
        self.last_finish_reason = ""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"LLM returned an invalid JSON response ({self.model} @ {self.base_url})"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"LLM HTTP failed ({self.model} @ {self.base_url}): {exc}"
            ) from exc
        try:
            self.last_finish_reason = str(body["choices"][0].get("finish_reason") or "")
        except (KeyError, IndexError, TypeError, AttributeError):
            self.last_finish_reason = ""
        return completion_text(body)


def resolve_model_id(configured: str, ids: list[str]) -> str:
    """Map a configured model name to a live id from GET /models.

    Empty ids keep the configured value (stripped). Blank / auto / * pick
    the first served id. Exact match keeps configured; any other mismatch
    rematches to the first served id so chat/completions hits a real model.
    """
    stripped = (configured or "").strip()
    if not ids:
        return stripped
    if stripped.casefold() in {"", "auto", "*"}:
        return ids[0]
    if stripped in ids:
        return stripped
    return ids[0]


def probe_models(base_url: str, api_key: str, *, timeout: float = 5) -> dict[str, Any]:
    """GET {base}/models. Raises RuntimeError on transport failure."""
    url = f"{base_url.rstrip('/')}/models"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {api_key or 'test'}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"data": [], "missing_ok": True}
        raise RuntimeError(f"models probe HTTP {exc.code} at {url}: {exc}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"unreachable at {url}: {exc}") from exc
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"data": []}
    return body if isinstance(body, dict) else {"data": []}


def write_project_file(repo: Path, rel: str, content: str, *, role: str) -> Path:
    if role != "writer":
        raise SafetyError("only the writer may edit the project body")
    if is_meta_path(rel):
        raise SafetyError("writer may not edit .nightshift/ meta files")
    path = assert_inside_repo(repo, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def apply_patch_hunk(repo: Path, rel: str, old: str, new: str, *, role: str) -> Path:
    if role != "writer":
        raise SafetyError("only the writer may edit the project body")
    if is_meta_path(rel):
        raise SafetyError("writer may not edit .nightshift/ meta files")
    path = assert_inside_repo(repo, rel)
    if not path.is_file():
        if len(new) > MAX_FULL_FILE_CHARS:
            raise SafetyError(
                f"patch target missing: {rel}; files[] content {len(new)} chars"
            )
        return write_project_file(repo, rel, new, role=role)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SafetyError(f"cannot read {rel} as utf-8: {exc}") from exc
    if len((old or "").strip()) < 3:
        raise SafetyError("patch old is degenerate; copy at least one full line verbatim")
    n = text.count(old)
    if n == 0:
        first = (old.splitlines() or [old])[0]
        close = difflib.get_close_matches(first, text.splitlines(), n=1, cutoff=0.6)
        extra = ""
        if close:
            line_no = text.splitlines().index(close[0]) + 1
            extra = f"; closest line {line_no}: {close[0]!r}"
        raise SafetyError(f"patch hunk not found in {rel}{extra}")
    if n > 1:
        raise SafetyError(f"patch hunk is not unique in {rel} ({n} matches)")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def persist_meta(repo: Path, rel: str, content: str) -> Path:
    if not is_meta_path(rel):
        raise SafetyError("meta persist is limited to .nightshift/")
    path = assert_inside_repo(repo, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def compose_widget_py(repo: Path, *, fix_add: bool = False, add_greet: bool = False) -> str:
    existing = ""
    target = repo / "widget.py"
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
    has_greet = add_greet or "def greet" in existing
    add_ok = fix_add or (
        "def add" in existing and "return a + b\n" in existing and "a + b + 1" not in existing
    )
    add_fn = (
        "def add(a, b):\n    return a + b\n"
        if add_ok
        else "def add(a, b):\n    return a + b + 1\n"
    )
    greet_fn = '\ndef greet(name):\n    return f"hello {name}"\n' if has_greet else ""
    return add_fn + greet_fn


def mock_upgrades_from_repo(repo: Path, size: int = BRIEF_SIZE_DEFAULT) -> list[Upgrade]:
    py = sys.executable
    found: list[tuple[str, str]] = []
    tests_dir = repo / "tests"
    if tests_dir.is_dir():
        for tf in sorted(tests_dir.glob("test_*.py")):
            text = tf.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"^def (test_\w+)", text, re.M):
                found.append((tf.relative_to(repo).as_posix(), match.group(1)))
    upgrades: list[Upgrade] = []
    for test_path, func in found[:size]:
        paths = ["widget.py"]
        if "version" in func:
            paths = ["VERSION"]
        elif "smoke" in func:
            paths = ["tests/test_smoke.py", "smoke.py"]
        upgrades.append(
            Upgrade(
                id=len(upgrades) + 1,
                title=f"Make {func} pass",
                check_command=f"{py} -m pytest {test_path}::{func} -q --rootdir=.",
                paths=paths,
            )
        )
    while len(upgrades) < size:
        n = len(upgrades) + 1
        if not found and n == 1:
            upgrades.append(
                Upgrade(
                    id=1,
                    title="Add a smoke test that fails then make it pass",
                    check_command=f"{py} -m pytest tests/test_smoke.py -q --rootdir=.",
                    paths=["tests/test_smoke.py", "smoke.py"],
                )
            )
            continue
        marker = f"NIGHTSHIFT_OK_{n}"
        fname = f"{marker}.txt"
        upgrades.append(
            Upgrade(
                id=n,
                title=f"Create {fname} containing {marker}",
                check_command=(
                    f"{py} -c \"from pathlib import Path; "
                    f"t=Path('{fname}').read_text(); assert '{marker}' in t\""
                ),
                paths=[fname],
            )
        )
    return upgrades[:size]


@dataclass
class MockChatClient:
    """Offline stand-in for Spark DS4 / Mac oMLX. No sockets."""

    role: str
    repo: Path
    mock: bool = True
    last_finish_reason: str = ""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> str:
        system = " ".join(m["content"] for m in messages if m["role"] == "system").lower()
        user = messages[-1]["content"] if messages else ""
        if self.role == "writer":
            return json.dumps(self._writer_payload(user))
        if "frozen brief" in system or "exactly" in system:
            m = re.search(r"exactly (\d+) upgrades", system, re.I)
            size = int(m.group(1)) if m else 3
            items = mock_upgrades_from_repo(self.repo, size=size)
            return json.dumps({"upgrades": [u.to_dict() for u in items]})
        return json.dumps({"passed_ids": [], "revert_paths": [], "notes": []})

    def _writer_payload(self, user: str) -> dict[str, Any]:
        job = user
        if "Current job:" in user:
            job = user.split("Current job:", 1)[1]
            if "Current job paths" in job:
                job = job.split("Current job paths", 1)[0]
            elif "Repo snapshot" in job:
                job = job.split("Repo snapshot", 1)[0]
        low = job.lower()
        files: list[dict[str, str]] = []
        if "test_add" in low or "make test_add" in low or "add(1" in low:
            files.append(
                {
                    "path": "widget.py",
                    "content": compose_widget_py(self.repo, fix_add=True),
                }
            )
        elif "test_greet" in low or "greet" in low:
            files.append(
                {
                    "path": "widget.py",
                    "content": compose_widget_py(self.repo, add_greet=True),
                }
            )
        elif "test_version" in low or "version" in low:
            files.append({"path": "VERSION", "content": "1.0.0\n"})
        elif "smoke" in low:
            files.append({"path": "smoke.py", "content": 'def ping():\n    return "pong"\n'})
            files.append(
                {
                    "path": "tests/test_smoke.py",
                    "content": (
                        "from smoke import ping\n\n"
                        "def test_smoke():\n    assert ping() == \"pong\"\n"
                    ),
                }
            )
        else:
            m = re.search(r"NIGHTSHIFT_OK_\d+", job)
            if m:
                marker = m.group(0)
                files.append({"path": f"{marker}.txt", "content": marker + "\n"})
        return {"files": files, "message": f"mock {self.role} pass"}


WRITER_SYSTEM = """You are the Nightshift writer (Spark / DeepSeek-V4-Flash).
You are the only role allowed to edit files. You have no network.
Do the one job in the user message. Do not add upgrades. The brief is frozen.
Return JSON only:
{"patches": [{"path": "README.md", "old": "exact unique substring", "new": "replacement"}], "files": [{"path": "new.py", "content": "full contents"}], "message": "short commit subject"}
Patch only files shown in full under `## job file`. A job path with no `## job file` block does not exist: create it with files[]. For an existing job path under 8000 chars prefer files[] with the full new content (always for tests/); patches[] for larger files, and `old` must be one or more complete lines copied verbatim.
Prefer patches for edits to existing large files. Never dump a whole README, QUICKSTART, or any file over ~80 lines in files[].
files[] is for NEW or tiny files only. If the path does not already exist, you MUST use files[] with the full contents. Never patches[] a missing path.
old must appear exactly once in the current file.
Edit only the current job's paths[]. Host refuses any other project file. Empty paths[] means write nothing.
A tests/ path does not allow writing src/. No gold-plating. No new markdown essays.
Never write .env, API keys, tokens, or private keys. If the job asks for that, skip those paths and do the rest.
"""



def critic_brief_system(size: int) -> str:
    n = int(size)
    extra_rows = ',\n  '.join(
        ['{\"title\": \"...\", \"check_command\": \"...\", \"paths\": [\"file.py\"]}']
        + ['{\"title\": \"...\", \"check_command\": \"...\", \"paths\": [\"...\"]}'] * (n - 1)
    )
    return f"""You are the Nightshift critic (Mac oMLX / GLM-5.3-Flash).
Minute 0. You inspect only. You must never write the project body.
Emit a frozen brief: EXACTLY {n} upgrades. Each must be checkable by a host command
(pytest, a script, file-exists+content grep, npm test, ...). Not "cleaner architecture".
If the repo has no tests, one of the {n} may be "add a smoke test that fails then make it pass".
Never propose rotating, editing, committing, or reading secrets (.env, API keys, tokens, private keys).
Do not list those files in upgrade paths. Secret hygiene is a human job, not a Nightshift upgrade.
Pick {n} checkable code, test, or docs upgrades.
Return JSON only:
{{"upgrades": [
  {extra_rows}
]}}
Exactly {n} objects. Extra upgrades will be rejected.
"""


CRITIC_BRIEF_SYSTEM = critic_brief_system(3)


CRITIC_JOB_SYSTEM = """You are the Nightshift critic. Write one line: the next remaining brief item as a job for the writer.
Write the job for the single upgrade in the user message only. Do not pick another id.
If last_attempt is present, the job is the single concrete fix that turns the check green: name the failing test and the error.
Return JSON: {"upgrade_id": <that id>, "job": "one line"}
No file writes. Tell the writer to edit only that upgrade's paths[]. Never instruct writes outside those paths.
Never tell the writer to edit .env, keys, tokens, or credentials.
"""

CRITIC_SCORE_SYSTEM = """You are the Nightshift critic. You may inspect, score, slash, revert, halt.
You must never write the project body. Host check output is truth, not the writer's opinion.
Return JSON:
{"passed_ids": [1], "revert_paths": ["gold.py"], "notes": ["why"], "halt": false}
Only include an id in passed_ids if the host check for that upgrade actually passed.
The writer may only touch the current job's paths[]. If the turn wrote any other project file, reject the turn: list those files in revert_paths and do not pass the upgrade.
Revert files outside the current job's paths[] (gold-plating or side effects).
"""


def _feedback_block(feedback: dict[str, Any] | None, locked_id: int) -> str:
    if not feedback or int(feedback.get("upgrade_id") or 0) != int(locked_id):
        return ""
    lines = [f"## Previous attempt (turn {feedback.get('turn') or '?'}) failed"]
    cmd = str(feedback.get("command") or "")
    if cmd:
        lines.append(f"$ {cmd}")
    if "exit_code" in feedback:
        lines.append(f"exit={feedback.get('exit_code')}")
    output = str(feedback.get("output") or "").strip()
    if output:
        lines.append(output)
    refused = [str(x) for x in (feedback.get("writer_refused") or []) if str(x).strip()]
    if refused:
        lines.append("## Your hunks that did not apply")
        for note in refused:
            lines.append(f"- {note}")
    notes = [str(x) for x in (feedback.get("critic_notes") or []) if str(x).strip()]
    compile_errors = [str(x) for x in (feedback.get("compile_errors") or []) if str(x).strip()]
    if notes or compile_errors:
        lines.append("## Critic notes")
        for note in compile_errors + notes:
            lines.append(f"- {note}")
    lines.append("Fix this failure in the files shown below; do not start over.")
    return "\n".join(lines) + "\n\n"


class Writer:
    """The only brain that may edit the project body."""

    def __init__(self, client: ChatClient, repo: Path) -> None:
        self.client = client
        self.repo = repo

    def apply_job(
        self,
        job: str,
        brief: Brief,
        snapshot: str,
        *,
        job_upgrade_id: int = 0,
        feedback: dict[str, Any] | None = None,
    ) -> WriterResult:
        if getattr(self.client, "repo", None) is not None:
            self.client.repo = self.repo  # type: ignore[attr-defined]
        missing: list[str] = []
        locked = None
        if int(job_upgrade_id or 0):
            locked = next((u for u in brief.upgrades if u.id == int(job_upgrade_id)), None)
        if locked is None:
            locked = brief.remaining()[0] if brief.remaining() else None
        job_paths = list(locked.paths) if locked is not None else []
        if locked is not None:
            seen: set[str] = set()
            for rel in locked.paths:
                norm = str(rel or "").replace(chr(92), "/").strip()
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                if not (self.repo / Path(norm)).is_file():
                    missing.append(norm)
        miss_note = ""
        if missing:
            joined = ", ".join(missing)
            miss_note = (
                "These job paths do not exist yet. Create them with files[] "
                f"(full contents), never patches[]: {joined}\n\n"
            )
        fb = _feedback_block(feedback, locked.id if locked is not None else 0)
        user = (
            f"Frozen brief (do not add extra upgrades):\n{json.dumps(brief.to_dict(), indent=2)}\n\n"
            f"Current job:\n{job}\n\n"
            f"Current job paths[] (writes outside these are refused): {json.dumps(job_paths)}\n\n"
            f"{fb}"
            f"{miss_note}"
            f"Repo snapshot (truncated):\n{snapshot[:WRITER_SNAPSHOT_CHARS]}\n"
        )
        raw = ""
        payload: dict[str, Any] | None = None
        truncated = False
        for attempt in range(3):
            try:
                messages = [
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": user},
                ]
                max_tokens = 16384 if truncated else 8192
                if attempt and truncated:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Reply was cut off at the token limit; send fewer, smaller hunks",
                        }
                    )
                elif attempt:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Previous reply was not a JSON object. Return JSON only.",
                        }
                    )
                raw = self.client.chat(messages, max_tokens=max_tokens)
                finish = str(getattr(self.client, "last_finish_reason", "") or "")
                truncated = finish == "length"
            except TimeoutError as exc:
                return WriterResult(
                    written=[],
                    message="timeout",
                    raw="",
                    refused=[f"writer timed out ({exc}); will retry"],
                )
            except (urllib.error.URLError, RuntimeError, OSError) as exc:
                return WriterResult(
                    written=[],
                    message="retry",
                    raw="",
                    refused=[f"{exc}; will retry"],
                )
            try:
                payload = parse_json_object(raw)
                break
            except (ValueError, json.JSONDecodeError):
                payload = None
                if truncated:
                    continue
                continue
        if payload is None:
            if truncated:
                return WriterResult(
                    written=[],
                    message="truncated",
                    raw=raw,
                    refused=["writer reply truncated (finish_reason=length); will retry"],
                )
            snippet = re.sub(r"\s+", " ", raw or "")[:240]
            return WriterResult(
                written=[],
                message="non-json",
                raw=raw,
                refused=[f"writer returned non-JSON; will retry ({snippet!r})"],
            )
        payload.pop("upgrades", None)
        payload.pop("extra_upgrades", None)
        payload.pop("brief", None)
        written: list[str] = []
        refused: list[str] = []
        seen_hunks: set[tuple[str, str, str]] = set()
        for row in _rows(payload.get("patches")):
            if not isinstance(row, dict):
                continue
            rel = str(row.get("path") or "").strip()
            old = row.get("old")
            new = row.get("new")
            if not rel or old is None or new is None:
                continue
            key = (rel, str(old), str(new))
            if key in seen_hunks:
                continue
            seen_hunks.add(key)
            try:
                assert_job_path(rel, job_paths)
                apply_patch_hunk(self.repo, rel, str(old), str(new), role="writer")
            except SafetyError as exc:
                refused.append(f"{rel}: {exc}")
                continue
            except (OSError, UnicodeError) as exc:
                refused.append(f"{rel}: {exc}")
                continue
            norm = rel.replace(chr(92), "/")
            if norm not in written:
                written.append(norm)
        for row in _rows(payload.get("files")):
            if not isinstance(row, dict):
                continue
            rel = str(row.get("path") or "").strip()
            if not rel:
                continue
            content = row.get("content")
            if content is None:
                continue
            body = str(content)
            if len(body) > MAX_FULL_FILE_CHARS:
                refused.append(
                    f"{rel}: full-file payload {len(body)} chars; use patches[] for existing files"
                )
                continue
            try:
                assert_job_path(rel, job_paths)
                existing = assert_inside_repo(self.repo, rel)
                if existing.is_file():
                    with existing.open(encoding="utf-8") as source:
                        if len(source.read(MAX_FULL_FILE_CHARS + 1)) > MAX_FULL_FILE_CHARS:
                            raise SafetyError(
                                f"existing file exceeds {MAX_FULL_FILE_CHARS} chars; use patches[]"
                            )
                write_project_file(self.repo, rel, body, role="writer")
            except SafetyError as exc:
                refused.append(f"{rel}: {exc}")
                continue
            except (OSError, UnicodeError) as exc:
                refused.append(f"{rel}: {exc}")
                continue
            norm = rel.replace(chr(92), "/")
            if norm not in written:
                written.append(norm)
        return WriterResult(
            written=written,
            message=str(payload.get("message") or "writer pass")[:200],
            raw=raw,
            refused=refused,
        )


class Critic:
    """Inspect / score / slash / halt. There is no write tool on this class."""

    def __init__(self, client: ChatClient, repo: Path) -> None:
        self.client = client
        self.repo = repo

    def propose_brief(self, snapshot: str, size: int = BRIEF_SIZE_DEFAULT) -> list[Upgrade]:
        size = int(size)
        if getattr(self.client, "mock", False):
            return mock_upgrades_from_repo(self.repo, size=size)
        last_raw = ""
        parse_err: Exception | None = None
        user = snapshot[:200_000]
        for attempt in range(3):
            messages = [
                {"role": "system", "content": critic_brief_system(size)},
                {"role": "user", "content": user},
            ]
            if attempt:
                messages.append(
                    {
                        "role": "user",
                        "content": "Previous reply was not a JSON object. Return JSON only.",
                    }
                )
            try:
                last_raw = self.client.chat(messages)
            except (TimeoutError, urllib.error.URLError, RuntimeError, OSError) as exc:
                raise RuntimeError(f"critic freeze failed: {exc}") from exc
            try:
                data = parse_json_object(last_raw)
            except (ValueError, json.JSONDecodeError) as exc:
                parse_err = exc
                continue
            extras = data.get("upgrades") if isinstance(data.get("upgrades"), list) else []
            if len(extras) != size:
                raise FrozenBriefError(
                    f"brief must be exactly {size} items, critic proposed {len(extras)}"
                )
            return Brief.from_proposed(data, size=size).upgrades  # type: ignore[return-value]
        snippet = re.sub(r"\s+", " ", last_raw or "")[:400]
        raise ValueError(
            f"no JSON object in model output after 3 freeze attempts: {snippet!r}"
        ) from parse_err

    def job_line(
        self,
        brief: Brief,
        *,
        upgrade_id: int | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        remaining = brief.remaining()
        if not remaining:
            return 0, ""
        if upgrade_id:
            target = next((u for u in remaining if u.id == int(upgrade_id)), remaining[0])
        else:
            target = remaining[0]
        if getattr(self.client, "mock", False):
            return (
                target.id,
                f"{target.title} (upgrade {target.id}). Check: {target.check_command}",
            )
        payload: dict[str, Any] = {"upgrade": target.to_dict()}
        if feedback and int(feedback.get("upgrade_id") or 0) == target.id:
            output = str(feedback.get("output") or "")
            payload["last_attempt"] = {
                "turn": feedback.get("turn"),
                "exit_code": feedback.get("exit_code"),
                "output_tail": output[-1200:],
                "writer_refused": feedback.get("writer_refused") or [],
                "critic_notes": feedback.get("critic_notes") or [],
            }
        try:
            raw = self.client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            CRITIC_JOB_SYSTEM
                            + f"\nWrite the job for upgrade_id {target.id} only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, indent=2),
                    },
                ]
            )
            data = parse_json_object(raw)
            job = str(data.get("job") or "").strip() or target.title
        except (ValueError, json.JSONDecodeError, TimeoutError, urllib.error.URLError, RuntimeError, OSError):
            job = target.title
        return target.id, job

    def opinion(
        self, brief: Brief, diff: str, logs: str, *, job_upgrade_id: int = 0
    ) -> dict[str, Any]:
        empty = {"passed_ids": [], "revert_paths": [], "notes": [], "halt": False}
        if getattr(self.client, "mock", False):
            return dict(empty)
        current = next((u for u in brief.upgrades if u.id == int(job_upgrade_id or 0)), None)
        job_paths = list(current.paths) if current is not None else []
        try:
            raw = self.client.chat(
                [
                    {"role": "system", "content": CRITIC_SCORE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"current_job_id={int(job_upgrade_id or 0)}\n"
                            f"current_job_paths={json.dumps(job_paths)}\n"
                            f"Writes outside current_job_paths must be reverted; do not pass that upgrade.\n\n"
                            f"brief={json.dumps(brief.to_dict())}\n\n"
                            f"diff:\n{diff[-16_000:]}\n\n"
                            f"check logs:\n{logs[-16_000:]}"
                        ),
                    },
                ]
            )
            data = parse_json_object(raw)
        except (ValueError, json.JSONDecodeError, TimeoutError, urllib.error.URLError, RuntimeError, OSError):
            return dict(empty)
        if not isinstance(data, dict):
            data = {}
        return {
            "passed_ids": _as_id_list(data.get("passed_ids")),
            "revert_paths": _as_str_list(data.get("revert_paths")),
            "notes": _as_str_list(data.get("notes")),
            # Local providers sometimes stringify JSON booleans. In particular,
            # bool("false") must never end an otherwise healthy night.
            "halt": data.get("halt") is True or (
                isinstance(data.get("halt"), str)
                and data["halt"].strip().casefold() == "true"
            ),
        }
