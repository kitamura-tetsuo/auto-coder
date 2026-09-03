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
