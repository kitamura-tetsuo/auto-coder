"""Tests for PR-scoped persistent adversarial reviewer sessions."""

from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry, TestOracleGap


def test_sessions_are_isolated_by_pr_and_backend_and_survive_reload(tmp_path):
    path = tmp_path / "sessions.json"
    registry = ReviewerSessionRegistry(path)
    claude_123 = ReviewerSession("owner/repo", 123, "claude-review", "claude", "opus", "claude-s1", "sha-a")
    claude_124 = ReviewerSession("owner/repo", 124, "claude-review", "claude", "opus", "claude-s2", "sha-b")
    codex_123 = ReviewerSession("owner/repo", 123, "codex-review", "codex", "gpt", "codex-s1", "sha-c")

    registry.save(claude_123)
    registry.save(claude_124)
    registry.save(codex_123)
    reloaded = ReviewerSessionRegistry(path)

    assert reloaded.get("owner/repo", 123, "claude-review", "claude", "opus") == claude_123
    assert reloaded.get("owner/repo", 124, "claude-review", "claude", "opus") == claude_124
    assert reloaded.get("owner/repo", 123, "codex-review", "codex", "gpt") == codex_123
    assert reloaded.get("owner/repo", 123, "codex-review", "codex", "opus") is None


def test_remove_pr_removes_all_of_only_that_pr(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    registry.save(ReviewerSession("owner/repo", 123, "claude", "claude", "opus", "s1", "a"))
    registry.save(ReviewerSession("owner/repo", 123, "codex", "codex", "gpt", "s2", "b"))
    registry.save(ReviewerSession("owner/repo", 124, "claude", "claude", "opus", "s3", "c"))

    registry.remove_pr("owner/repo", 123)

    assert registry.get("owner/repo", 123, "claude", "claude", "opus") is None
    assert registry.get("owner/repo", 123, "codex", "codex", "gpt") is None
    assert registry.get("owner/repo", 124, "claude", "claude", "opus") is not None


def test_material_test_oracle_gap_identity_and_scope_survive_reload(tmp_path):
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    gap = TestOracleGap(
        gap_id="TOG-123",
        requirement_id="REQ-001",
        authoritative_boundary="GridMutation.apply_candidate",
        invariant="Rejection preserves stored state and revision",
        status="OPEN",
    )
    session = ReviewerSession("owner/repo", 123, "codex", "codex", "gpt", "s1", "sha-a", [gap])

    registry.save(session)

    assert ReviewerSessionRegistry(registry.path).get("owner/repo", 123, "codex", "codex", "gpt") == session
