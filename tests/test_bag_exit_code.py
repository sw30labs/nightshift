from __future__ import annotations

import pytest

from nightshift.bag import bag_exit_code


def _done(remaining_count):
    return {"targets": [{"state": "done", "remaining_count": remaining_count}]}


def test_done_with_zero_remaining_is_accepted():
    assert bag_exit_code(_done(0)) == 0


def test_all_done_with_zero_remaining_is_accepted():
    assert bag_exit_code(
        {
            "targets": [
                {"state": "done", "remaining_count": 0},
                {"state": "done", "remaining_count": 0},
            ]
        }
    ) == 0


def test_done_with_missing_remaining_count_is_accepted():
    assert bag_exit_code({"targets": [{"state": "done"}]}) == 0
    assert bag_exit_code(_done(None)) == 0


def test_done_with_positive_remaining_is_rejected():
    assert bag_exit_code(_done(1)) == 2
    assert bag_exit_code(_done(2)) == 2


def test_any_done_with_remaining_is_rejected():
    assert bag_exit_code(
        {
            "targets": [
                {"state": "done", "remaining_count": 0},
                {"state": "done", "remaining_count": 1},
            ]
        }
    ) == 2


@pytest.mark.parametrize("remaining_count", [-1, -99])
def test_done_with_negative_remaining_is_rejected(remaining_count):
    assert bag_exit_code(_done(remaining_count)) == 2


@pytest.mark.parametrize(
    "remaining_count",
    ["1", "abc", "", [], {}, 0.0, 1.0, object()],
)
def test_done_with_non_int_remaining_is_rejected(remaining_count):
    assert bag_exit_code(_done(remaining_count)) == 2


def test_bool_remaining_count_is_treated_as_int():
    assert bag_exit_code(_done(False)) == 0
    assert bag_exit_code(_done(True)) == 2


def test_error_and_skipped_are_rejected():
    assert bag_exit_code({"targets": [{"state": "error"}]}) == 2
    assert bag_exit_code({"targets": [{"state": "skipped"}]}) == 2
    assert bag_exit_code({"targets": [{"state": "error", "remaining_count": 0}]}) == 2
    assert bag_exit_code({"targets": [{"state": "skipped", "remaining_count": 0}]}) == 2


def test_interrupted_precedes_other_outcomes():
    assert bag_exit_code(
        {
            "interrupted": True,
            "main_touched": True,
            "targets": [{"state": "error"}],
        }
    ) == 130


def test_main_touched_precedes_target_outcomes():
    assert bag_exit_code(
        {"main_touched": True, "targets": [{"state": "error"}]}
    ) == 3


def test_no_targets_is_rejected():
    assert bag_exit_code({"targets": []}) == 1
    assert bag_exit_code({"targets": ["junk", None, 1]}) == 1
    assert bag_exit_code({}) == 1


def test_malformed_remaining_count_does_not_raise():
    for bad in (None, "abc", [], {}, -1, 0.0):
        bag_exit_code(_done(bad))
