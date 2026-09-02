"""OpenAI-compatible HTTP clients, mock provider, writer tools, critic (no write)."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import Brief, FrozenBriefError, SafetyError, Upgrade, WriterResult
from .safety import assert_inside_repo, is_meta_path


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
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"LLM HTTP failed ({self.model} @ {self.base_url}): {exc}"
            ) from exc
        return completion_text(body)


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
        raise SafetyError(f"patch target missing: {rel}")
    text = path.read_text(encoding="utf-8")
    n = text.count(old)
    if n == 0:
        raise SafetyError(f"patch hunk not found in {rel}")
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


def mock_upgrades_from_repo(repo: Path, size: int = 3) -> list[Upgrade]:
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
Prefer patches for edits to existing files. Never dump a whole README, QUICKSTART, or any file over ~80 lines in files[].
files[] is for NEW or tiny files only. old must appear exactly once in the current file.
Edit only paths that serve the current job. No gold-plating. No new markdown essays.
Never write .env, API keys, tokens, or private keys. If the job asks for that, skip those paths and do the rest.
"""

MAX_FULL_FILE_CHARS = 8_000


def critic_brief_system(size: int) -> str:
    n = int(size)
    extra_rows = '\n  '.join(
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
Return JSON: {"upgrade_id": 1, "job": "one line"}
No file writes. Never tell the writer to edit .env, keys, tokens, or credentials.
"""

CRITIC_SCORE_SYSTEM = """You are the Nightshift critic. You may inspect, score, slash, revert, halt.
You must never write the project body. Host check output is truth, not the writer's opinion.
Return JSON:
{"passed_ids": [1], "revert_paths": ["gold.py"], "notes": ["why"], "halt": false}
Only include an id in passed_ids if the host check for that upgrade actually passed.
Revert files outside the brief paths (gold-plating).
"""


class Writer:
    """The only brain that may edit the project body."""

    def __init__(self, client: ChatClient, repo: Path) -> None:
        self.client = client
        self.repo = repo

    def apply_job(self, job: str, brief: Brief, snapshot: str) -> WriterResult:
        if getattr(self.client, "repo", None) is not None:
            self.client.repo = self.repo  # type: ignore[attr-defined]
        user = (
            f"Frozen brief (do not add extra upgrades):\n{json.dumps(brief.to_dict(), indent=2)}\n\n"
            f"Current job:\n{job}\n\n"
            f"Repo snapshot (truncated):\n{snapshot[:120_000]}\n"
        )
        raw = ""
        payload: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                messages = [
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": user},
                ]
                if attempt:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Previous reply was not a JSON object. Return JSON only.",
                        }
                    )
                raw = self.client.chat(messages)
            except (TimeoutError, urllib.error.URLError) as exc:
                return WriterResult(
                    written=[],
                    message="timeout",
                    raw="",
                    refused=[f"writer timed out ({exc}); will retry"],
                )
            try:
                payload = parse_json_object(raw)
                break
            except (ValueError, json.JSONDecodeError):
                payload = None
                continue
        if payload is None:
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
        for row in payload.get("patches") or []:
            if not isinstance(row, dict):
                continue
            rel = str(row.get("path") or "").strip()
            old = row.get("old")
            new = row.get("new")
            if not rel or old is None or new is None:
                continue
            try:
                apply_patch_hunk(self.repo, rel, str(old), str(new), role="writer")
            except SafetyError as exc:
                refused.append(f"{rel}: {exc}")
                continue
            norm = rel.replace(chr(92), "/")
            if norm not in written:
                written.append(norm)
        for row in payload.get("files") or []:
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
                write_project_file(self.repo, rel, body, role="writer")
            except SafetyError as exc:
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

    def propose_brief(self, snapshot: str, size: int = 3) -> list[Upgrade]:
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
            last_raw = self.client.chat(messages)
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

    def job_line(self, brief: Brief) -> tuple[int, str]:
        remaining = brief.remaining()
        if not remaining:
            return 0, ""
        target = remaining[0]
        if getattr(self.client, "mock", False):
            return (
                target.id,
                f"{target.title} (upgrade {target.id}). Check: {target.check_command}",
            )
        raw = self.client.chat(
            [
                {"role": "system", "content": CRITIC_JOB_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"remaining": [u.to_dict() for u in remaining]}, indent=2
                    ),
                },
            ]
        )
        data = parse_json_object(raw)
        uid = int(data.get("upgrade_id") or target.id)
        job = str(data.get("job") or target.title)
        return uid, job

    def opinion(self, brief: Brief, diff: str, logs: str) -> dict[str, Any]:
        if getattr(self.client, "mock", False):
            return {"passed_ids": [], "revert_paths": [], "notes": [], "halt": False}
        raw = self.client.chat(
            [
                {"role": "system", "content": CRITIC_SCORE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"brief={json.dumps(brief.to_dict())}\n\n"
                        f"diff:\n{diff[-16_000:]}\n\n"
                        f"check logs:\n{logs[-16_000:]}"
                    ),
                },
            ]
        )
        try:
            data = parse_json_object(raw)
        except (ValueError, json.JSONDecodeError):
            data = {}
        return {
            "passed_ids": [int(x) for x in (data.get("passed_ids") or [])],
            "revert_paths": [str(x) for x in (data.get("revert_paths") or [])],
            "notes": [str(x) for x in (data.get("notes") or [])],
            "halt": bool(data.get("halt", False)),
        }
