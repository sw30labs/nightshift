from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.models import SafetyError
from nightshift.safety import assert_safe_target, is_nightshift_repo


def test_nightshift_repo_detection():
    here = Path(__file__).resolve().parents[1]
    assert is_nightshift_repo(here)
    with pytest.raises(SafetyError, match="own repo"):
        assert_safe_target(here, explicit=False)
    resolved = assert_safe_target(here, explicit=True)
    assert resolved == here.resolve()
