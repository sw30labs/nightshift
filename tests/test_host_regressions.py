import json
import sys

from nightshift.host import _INTERP_CACHE, count_failed, needs_shell, resolve_interpreter, rewrite_shell_command, run_check
from nightshift.models import Upgrade


def _check(repo, command):
    return run_check(repo, Upgrade(1, "regression", command, ["file.txt"]), timeout=10)


def test_python_quoted_punctuation_does_not_require_shell():
    assert not needs_shell('python -c "print(\'hello; world!\')"')
    assert needs_shell('python -c "print(\'ok\')" && echo done')


def test_shell_rewrite_leaves_quoted_python_source_untouched(tmp_path):
    command = 'python -c "print(\'python -c\')" && echo done'
    rewritten = rewrite_shell_command(command, tmp_path)
    assert "print('python -c')" in rewritten
    result = _check(tmp_path, command)
    assert result.ok, result.output
    assert result.output == "python -c\ndone\n"


def test_python_semicolon_code_runs_without_mutating_literals(tmp_path):
    result = _check(tmp_path, 'python -c "value = \'python -c\'; print(value)"')
    assert result.ok, result.output
    assert result.output == "python -c\n"


def test_pipeline_failure_cannot_be_hidden_by_successful_last_command(tmp_path):
    result = _check(tmp_path, "false | cat")
    assert not result.ok
    assert result.exit_code == 1


def test_non_utf8_output_is_preserved_as_replacement_characters(tmp_path):
    result = _check(tmp_path, 'python -c "import os; os.write(1, bytes([255, 10]))"')
    assert result.ok, result.output
    assert "\ufffd" in result.output


def test_interpreter_environment_override_is_not_stale(tmp_path, monkeypatch):
    _INTERP_CACHE.clear()
    monkeypatch.setenv("NIGHTSHIFT_TARGET_PYTHON", "/old/python")
    assert resolve_interpreter(tmp_path).path == "/old/python"
    monkeypatch.setenv("NIGHTSHIFT_TARGET_PYTHON", sys.executable)
    assert resolve_interpreter(tmp_path).path == sys.executable
    monkeypatch.delenv("NIGHTSHIFT_TARGET_PYTHON")
    assert resolve_interpreter(tmp_path).source != "override"


def test_non_object_host_configuration_does_not_crash_checks(tmp_path):
    meta = tmp_path / ".nightshift"
    meta.mkdir()
    (meta / "host.json").write_text(json.dumps(["invalid shape"]))
    result = _check(tmp_path, "python -c 'print(123)'")
    assert result.ok, result.output
    assert "123" in result.output


def test_failure_counts_include_failures_and_errors():
    assert count_failed("2 failed, 3 passed, 1 error in 0.10s") == 3
