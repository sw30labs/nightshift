from __future__ import annotations

import sys

import pytest

from nightshift.host import argv_for, run_check
from nightshift.models import Upgrade

_DQ = chr(34)
_SQ = chr(39)
_MALFORMED = [
    '',
    '   ',
    f'pytest {_DQ}tests/test_ok.py',
    f'pytest {_SQ}tests/test_ok.py',
    f'python -c {_DQ}print(1)',
    f'pytest {_DQ}tests/test_ok.py; echo hi',
]


@pytest.mark.parametrize('command', _MALFORMED)
def test_argv_for_rejects_malformed_commands(command: str) -> None:
    with pytest.raises(ValueError):
        argv_for(command)


@pytest.mark.parametrize('command', _MALFORMED)
def test_run_check_fails_closed_on_malformed_commands(
    tmp_path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv('NIGHTSHIFT_TARGET_PYTHON', sys.executable)
    called: list[bool] = []

    def fake_popen(*args, **kwargs):
        called.append(True)
        raise AssertionError('malformed host check must not spawn a process')

    monkeypatch.setattr('nightshift.host.subprocess.Popen', fake_popen)

    upgrade = Upgrade(
        1,
        'malformed host check',
        command,
        ['tests/test_host_check_malformed.py'],
    )
    result = run_check(tmp_path, upgrade, timeout=1)

    assert called == []
    assert result.upgrade_id == 1
    assert result.command == command
    assert result.ok is False
    assert result.exit_code == -1
    assert result.output
    if command.strip() == '':
        assert 'empty' in result.output.lower()
    else:
        assert 'closing' in result.output.lower() or 'quotation' in result.output.lower()
