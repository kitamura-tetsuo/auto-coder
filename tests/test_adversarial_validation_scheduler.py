"""Admission regressions for adversarial-validation scheduling."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.adversarial_validation_scheduler import AdversarialValidationScheduler


@pytest.mark.parametrize("limit", [0, -1, True, None])
def test_scheduler_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        AdversarialValidationScheduler(limit)  # type: ignore[arg-type]


def test_independent_config_defaults_to_two_and_supports_serial_execution(tmp_path) -> None:
    from auto_coder.llm_backend_config import get_adversarial_validation_concurrency_from_config

    default_config = tmp_path / "default.toml"
    default_config.write_text("[process_issues]\nvalidation_concurrency = 5\n", encoding="utf-8")
    assert get_adversarial_validation_concurrency_from_config(str(default_config)) == 2

    serial_config = tmp_path / "serial.toml"
    serial_config.write_text("[process_issues]\nadversarial_validation_concurrency = 1\n", encoding="utf-8")
    assert get_adversarial_validation_concurrency_from_config(str(serial_config)) == 1


def test_two_slots_bound_three_distinct_prs_and_release_exactly_one() -> None:
    scheduler = AdversarialValidationScheduler(2)
    entered = threading.Barrier(3)
    release_first = threading.Event()
    third_entered = threading.Event()
    active = 0
    maximum = 0
    lock = threading.Lock()

    def validation(number: int) -> None:
        nonlocal active, maximum
        with scheduler.admit("owner/repo", number) as lease:
            assert lease.acquired
            with lock:
                active += 1
                maximum = max(maximum, active)
            if number in (1, 2):
                entered.wait(timeout=2)
                if number == 1:
                    release_first.wait(timeout=2)
                else:
                    release_first.wait(timeout=2)
            else:
                third_entered.set()
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(validation, 1)
        second = pool.submit(validation, 2)
        entered.wait(timeout=2)
        third = pool.submit(validation, 3)
        assert not third_entered.wait(0.1)
        release_first.set()
        assert third_entered.wait(2)
        first.result(timeout=2)
        second.result(timeout=2)
        third.result(timeout=2)

    assert maximum == 2


def test_duplicate_pr_is_not_queued_for_a_later_attempt() -> None:
    scheduler = AdversarialValidationScheduler(2)
    with scheduler.admit("owner/repo", 7) as first:
        assert first.acquired
        with scheduler.admit("owner/repo", 7) as duplicate:
            assert not duplicate.acquired
    with scheduler.admit("owner/repo", 7) as later:
        assert later.acquired
