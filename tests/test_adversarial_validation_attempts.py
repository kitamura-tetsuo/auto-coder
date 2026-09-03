from concurrent.futures import ThreadPoolExecutor
from threading import Event

from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository


def test_overlapping_attempts_keep_independent_identity_and_newest_publication(tmp_path):
    repository = AdversarialValidationAttemptRepository("owner/repo", tmp_path / "attempts.json")

    older = repository.start(100, "head-a")
    newer = repository.start(100, "head-a")
    repository.finish(newer.attempt_id, "PASS")
    repository.mark_published(newer.attempt_id)
    repository.finish(older.attempt_id, "ERROR")
    repository.mark_published(older.attempt_id)

    assert older.attempt_id != newer.attempt_id
    assert older.sequence < newer.sequence
    assert repository.latest_published_sequence(100, "head-a") == newer.sequence


def test_publication_cannot_cross_a_final_merge_transition(tmp_path):
    path = tmp_path / "attempts.json"
    merge_repository = AdversarialValidationAttemptRepository("owner/repo", path)
    publication_repository = AdversarialValidationAttemptRepository("owner/repo", path)
    merge_entered = Event()
    release_merge = Event()
    publication_entered = Event()

    def merge_transition():
        with merge_repository.serialized_transition():
            merge_entered.set()
            assert release_merge.wait(timeout=2)

    def publication_transition():
        assert merge_entered.wait(timeout=2)
        with publication_repository.serialized_transition():
            publication_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        merge_future = executor.submit(merge_transition)
        publication_future = executor.submit(publication_transition)
        assert merge_entered.wait(timeout=2)
        assert not publication_entered.wait(timeout=0.1)
        release_merge.set()
        merge_future.result(timeout=2)
        publication_future.result(timeout=2)

    assert publication_entered.is_set()
