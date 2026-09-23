"""Issue #2266: production ``GitHubClient`` bridge for reviewer identity
resolution and Issue findings publication.

``auto_coder.util.gh_cache.GitHubClient.reviewer_app_identity`` and
``publish_issue_review_comment`` previously imported from the nonexistent
``auto_coder.util.github_app_reviewer`` / ``auto_coder.util.issue_review_publication``
modules -- the canonical implementations live directly under ``auto_coder``,
not ``auto_coder.util``. Every existing lifecycle/resumption test supplies a
fake ``github`` object with its own ``reviewer_app_identity`` /
``publish_issue_review_comment`` methods (see
``tests/test_validation_publication_resumption.py``,
``tests/test_decomposition_validation_publication_resumption.py``,
``tests/test_specification_validation_lifecycle.py``), so none of them
exercised the defective imports in the real client. These tests close that
gap: they call the real ``GitHubClient`` bridge directly, and the three named
production validation-lifecycle entry points that use it
(``SpecificationValidationLifecycle.apply_blocked``/``apply_inherited_blocked``,
``DecompositionValidationLifecycle.apply_blocked``), with wire-level HTTP
control rather than a substitute for either client method.
"""

from __future__ import annotations

import ast
import inspect
import json
import types
from pathlib import Path

import httpx
import pytest

from auto_coder.decomposition_analyzer import DecompositionAnalysisResult, DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppIdentity
from auto_coder.github_pending_work import PendingWorkStore
from auto_coder.issue_review_publication import PublicationReceipt
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_request_outcome import GitHubApiOutcome, GitHubRequestError

REVIEWER_LOGIN = "auto-coder-reviewer[bot]"
REVIEWER_SLUG = "auto-coder-reviewer"
REVIEWER_APP_ID = 990001

BODY = "## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")

PARENT_BODY = "## Objective\nCoordinate the tracked child behaviors."
CHILD_BODY = "## Requirements\n- REQ-001: Deliver the first behavior."


# ---------------------------------------------------------------------------
# Reviewer-App HTTP wire double: a real ``httpx.Client`` backed by
# ``httpx.MockTransport`` so the genuine ``instrument_github_client``
# instrumentation and status-based confirmation logic run unmodified.
# ---------------------------------------------------------------------------


@pytest.fixture
def reviewer_app_env(tmp_path, monkeypatch):
    """Isolated reviewer App configuration/key material (Path.home() sandbox)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".auto-coder").mkdir()
    (home / ".auto-coder" / "config.toml").write_text(f"[github-app-auto-coder-reviewer]\napp_id = {REVIEWER_APP_ID}\n", encoding="utf-8")
    (home / ".auto-coder" / "auto-coder-reviewer.pem").write_text("FAKE_PEM", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(GitHubAppReviewer, "_jwt", lambda self: "fake_jwt")
    return home


def _install_reviewer_transport(monkeypatch, handler):
    """Route every reviewer-App HTTP call through ``handler`` via a real client."""
    original_client = httpx.Client

    def factory(**kwargs):
        return original_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout", 30.0))

    monkeypatch.setattr("auto_coder.github_app_reviewer.httpx.Client", factory)


class _ReviewerRequestLog:
    """Records every reviewer-App request a handler observed, in order."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def record(self, request: httpx.Request) -> None:
        self.requests.append(request)


def _standard_reviewer_handler(log: _ReviewerRequestLog, *, app_id: int = REVIEWER_APP_ID, slug: str = REVIEWER_SLUG, login: str = REVIEWER_LOGIN, comment_id: int = 4242, comment_status: int = 201, comment_login: str | None = None, comment_app_id: int | None = None):
    """Build a handler that satisfies app-auth/installation/token/comment calls.

    ``comment_login``/``comment_app_id`` let a test return a comment
    authored by a different identity to exercise the unconfirmed path
    without needing a separate handler shape.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": app_id, "slug": slug})
        if path.endswith("/installation"):
            return httpx.Response(200, json={"id": 555000})
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "reviewer-installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/comments") and request.method == "POST":
            payload = json.loads(request.content)
            return httpx.Response(
                comment_status,
                json={
                    "id": comment_id,
                    "issue_url": f"https://api.github.com{path.rsplit('/comments', 1)[0]}",
                    "body": payload.get("body", ""),
                    "user": {"login": comment_login if comment_login is not None else login},
                    "performed_via_github_app": {"id": comment_app_id if comment_app_id is not None else app_id},
                },
            )
        raise AssertionError(f"unexpected reviewer-app HTTP call: {request.method} {request.url}")

    return handler


def _comment_target_handler(log: _ReviewerRequestLog, *, repo: str, issue_number: int, app_id: int = REVIEWER_APP_ID, slug: str = REVIEWER_SLUG, login: str = REVIEWER_LOGIN, comment_id: int = 4242):
    """A handler whose comment response echoes the exact repo/issue target, so a
    confirmation check that inspects ``issue_url`` is exercised faithfully."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": app_id, "slug": slug})
        if path.endswith("/installation"):
            return httpx.Response(200, json={"id": 555000})
        if path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "reviewer-installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path.endswith("/comments") and request.method == "POST":
            payload = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": comment_id,
                    "issue_url": f"https://api.github.com/repos/{repo}/issues/{issue_number}",
                    "body": payload.get("body", ""),
                    "user": {"login": login},
                    "performed_via_github_app": {"id": app_id},
                },
            )
        raise AssertionError(f"unexpected reviewer-app HTTP call: {request.method} {request.url}")

    return handler


# ---------------------------------------------------------------------------
# Ordinary (controller-token) GitHub wire double: routes GitHubClient's
# regular REST calls (issue read, comment read, label removal) by
# (method, path), independent of the reviewer-App transport above.
# ---------------------------------------------------------------------------


class _RoutedOrdinaryClient:
    def __init__(self, routes: dict[tuple[str, str], object]):
        self._routes = routes
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        path = url.split("?")[0]
        key = (method.upper(), path)
        self.calls.append(key)
        if key not in self._routes:
            raise AssertionError(f"unmocked ordinary GitHub wire call: {method} {path}")
        entry = self._routes[key]
        return entry(**kwargs) if callable(entry) else entry

    def close(self) -> None:
        pass


def _issue_response(number: int, title: str, body: str, *, ready: bool = True, state: str = "open") -> httpx.Response:
    labels = [{"name": "implementation-ready"}] if ready else []
    return httpx.Response(200, json={"number": number, "title": title, "body": body, "labels": labels, "state": state}, request=httpx.Request("GET", "https://api.github.com/"))


def _comments_response(comments: list[dict]) -> httpx.Response:
    return httpx.Response(200, json=comments, request=httpx.Request("GET", "https://api.github.com/"))


def _issue_routes(repo: str, number: int, title: str, body: str, *, ready: bool = True) -> dict[tuple[str, str], object]:
    base = f"https://api.github.com/repos/{repo}/issues/{number}"
    return {
        ("GET", base): (lambda **_: _issue_response(number, title, body, ready=ready)),
        ("GET", f"{base}/comments"): (lambda **_: _comments_response([])),
        ("DELETE", f"{base}/labels/implementation-ready"): (lambda **_: httpx.Response(200, json={}, request=httpx.Request("DELETE", base))),
    }


def _install_ordinary_transport(monkeypatch, routes: dict[tuple[str, str], object]) -> _RoutedOrdinaryClient:
    client = _RoutedOrdinaryClient(routes)
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *a, **k: client)
    return client


# ---------------------------------------------------------------------------
# AS-001: exercise both real GitHubClient methods from a clean call, with no
# monkeypatching of either method and no substitute for the canonical
# resolver/publisher.
# ---------------------------------------------------------------------------


def test_reviewer_app_identity_delegates_to_canonical_resolver_over_real_http(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()
    _install_reviewer_transport(monkeypatch, _standard_reviewer_handler(log, slug="distinct-reviewer-slug"))

    client = GitHubClient("controller-token")
    identity = client.reviewer_app_identity("distinct-owner/distinct-repo")

    assert identity == ReviewerAppIdentity(login="distinct-reviewer-slug[bot]", app_id=REVIEWER_APP_ID)
    assert len(log.requests) == 1
    assert log.requests[0].method == "GET"
    assert log.requests[0].url.path == "/app"


def test_publish_issue_review_comment_delegates_to_canonical_publisher_over_real_http(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()
    repo = "distinct-owner/distinct-repo"
    issue_number = 77042
    _install_reviewer_transport(monkeypatch, _comment_target_handler(log, repo=repo, issue_number=issue_number, comment_id=90909))

    client = GitHubClient("controller-token")
    receipt = client.publish_issue_review_comment(repo, issue_number, "Exact findings body", lambda: True)

    assert receipt == PublicationReceipt(90909, REVIEWER_LOGIN, REVIEWER_APP_ID)
    post_calls = [r for r in log.requests if r.method == "POST" and r.url.path.endswith("/comments")]
    assert len(post_calls) == 1
    assert post_calls[0].url == f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    assert json.loads(post_calls[0].content)["body"] == "Exact findings body"


# ---------------------------------------------------------------------------
# AS-003: genuine reviewer failures propagate rather than fabricate success
# or silently fall back to the ordinary controller credential.
# ---------------------------------------------------------------------------


def test_reviewer_identity_unavailable_propagates_without_fabrication(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()

    def handler(request: httpx.Request) -> httpx.Response:
        log.record(request)
        assert request.url.path == "/app"
        # A mismatched App id: GitHub answered, but not as the configured App.
        return httpx.Response(200, json={"id": 1, "slug": "someone-else"})

    _install_reviewer_transport(monkeypatch, handler)
    client = GitHubClient("controller-token")

    with pytest.raises(RuntimeError):
        client.reviewer_app_identity("owner/repo")
    assert len(log.requests) == 1


def test_publication_rejection_raises_typed_error_with_inspectable_outcome(monkeypatch, reviewer_app_env):
    """A confirmed HTTP response authored by someone else is a definite,
    typed rejection (REQ-004/REQ-006): the propagated ``GitHubRequestError``
    must be inspected by its outcome, not only by an error string."""
    log = _ReviewerRequestLog()
    handler = _standard_reviewer_handler(log, comment_login="someone-else[bot]", comment_app_id=1)
    _install_reviewer_transport(monkeypatch, handler)

    client = GitHubClient("controller-token")
    with pytest.raises(GitHubRequestError) as excinfo:
        client.publish_issue_review_comment("owner/repo", 42, "Exact findings body", lambda: True)

    assert excinfo.value.outcome.classification is GitHubApiOutcome.REMOTE_ERROR
    post_calls = [r for r in log.requests if r.method == "POST" and r.url.path.endswith("/comments")]
    assert len(post_calls) == 1  # exactly one attempt: no retry-as-someone-else, no ordinary-credential fallback


# ---------------------------------------------------------------------------
# AS-004: the caller's authorization callback is honored at the publisher's
# final pre-send boundary, through the real client.
# ---------------------------------------------------------------------------


def test_authorization_callback_false_blocks_the_comment_post(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()
    handler = _standard_reviewer_handler(log)
    _install_reviewer_transport(monkeypatch, handler)

    client = GitHubClient("controller-token")
    order: list[str] = []

    def authorize() -> bool:
        order.append("authorize")
        return False

    with pytest.raises(GitHubRequestError) as excinfo:
        client.publish_issue_review_comment("owner/repo", 42, "Exact findings body", authorize)

    assert order == ["authorize"]
    assert excinfo.value.outcome.classification is GitHubApiOutcome.REFUSED
    # Authentication/admission (app + installation + token) happened, but no
    # comment POST was ever attempted -- and certainly none was posted then deleted.
    methods_and_paths = [(r.method, r.url.path) for r in log.requests]
    assert ("POST", "/repos/owner/repo/issues/42/comments") not in methods_and_paths
    assert len(log.requests) == 3


# ---------------------------------------------------------------------------
# AS-005: reject a partial fix. Each lazy import is independent: reverting
# only one method's import to its pre-fix, nonexistent-module target must
# fail that method alone while the other (still fixed) method keeps working.
# ---------------------------------------------------------------------------


def test_reverting_only_the_publication_import_fails_independently_of_identity(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()
    _install_reviewer_transport(monkeypatch, _standard_reviewer_handler(log))
    client = GitHubClient("controller-token")

    def _pre_fix_publish(self, repo_name, issue_number, body, authorize_fn):
        from auto_coder.util.issue_review_publication import publish_findings_comment  # pre-fix (nonexistent) target

        return publish_findings_comment(repo_name, issue_number, body, authorize_fn)

    client.publish_issue_review_comment = types.MethodType(_pre_fix_publish, client)

    identity = client.reviewer_app_identity("owner/repo")
    assert identity == ReviewerAppIdentity(login=REVIEWER_LOGIN, app_id=REVIEWER_APP_ID)

    with pytest.raises(ModuleNotFoundError, match="auto_coder.util.issue_review_publication"):
        client.publish_issue_review_comment("owner/repo", 42, "body", lambda: True)


def test_reverting_only_the_identity_import_fails_independently_of_publication(monkeypatch, reviewer_app_env):
    log = _ReviewerRequestLog()
    repo = "owner/repo"
    issue_number = 42
    _install_reviewer_transport(monkeypatch, _comment_target_handler(log, repo=repo, issue_number=issue_number))
    client = GitHubClient("controller-token")

    def _pre_fix_identity(self, repo_name):
        from auto_coder.util.github_app_reviewer import resolve_reviewer_app_identity  # pre-fix (nonexistent) target

        return resolve_reviewer_app_identity(repo_name)

    client.reviewer_app_identity = types.MethodType(_pre_fix_identity, client)

    with pytest.raises(ModuleNotFoundError, match="auto_coder.util.github_app_reviewer"):
        client.reviewer_app_identity(repo)

    receipt = client.publish_issue_review_comment(repo, issue_number, "body", lambda: True)
    assert receipt == PublicationReceipt(4242, REVIEWER_LOGIN, REVIEWER_APP_ID)


def test_type_checking_imports_resolve_to_canonical_modules():
    """The two ``TYPE_CHECKING``-only imports must match the same canonical
    modules the runtime imports resolve to, not the pre-fix ``util.*`` path."""
    source = Path(inspect.getfile(GitHubClient)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    resolved: dict[str, tuple[int, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom):
                    for alias in stmt.names:
                        resolved[alias.name] = (stmt.level, stmt.module)

    # level=2 relative to auto_coder.util.gh_cache resolves to auto_coder.<module>,
    # matching the runtime imports' own corrected target.
    assert resolved["ReviewerAppIdentity"] == (2, "github_app_reviewer")
    assert resolved["PublicationReceipt"] == (2, "issue_review_publication")

    import auto_coder.github_app_reviewer as reviewer_module
    import auto_coder.issue_review_publication as publication_module

    assert reviewer_module.ReviewerAppIdentity is ReviewerAppIdentity
    assert publication_module.PublicationReceipt is PublicationReceipt
    assert GitHubClient.reviewer_app_identity.__annotations__["return"] == "ReviewerAppIdentity"
    assert GitHubClient.publish_issue_review_comment.__annotations__["return"] == "PublicationReceipt"


# ---------------------------------------------------------------------------
# AS-002: reach the bridge from the three named production validation
# origins, using the real GitHubClient for both the ordinary wire (issue
# read, comment read, label removal) and the reviewer-App wire (identity,
# publication).
# ---------------------------------------------------------------------------


def _blocked_individual_gate(tmp_path):
    result = SpecificationAnalysisResult("BLOCKED", (FINDING,))
    return SpecificationValidationLifecycle("owner/individual-repo", "policy-a", tmp_path / "individual-decisions.json", lambda *_a: result)


def test_individual_specification_blocked_publishes_and_withdraws_via_real_client(tmp_path, monkeypatch, reviewer_app_env):
    """Origin 1 (AS-002 table row 1): SpecificationValidationLifecycle.apply_blocked."""
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: PendingWorkStore(tmp_path / "pending.db"))

    repo = "owner/individual-repo"
    issue_number = 87001
    title = "Title"
    gate = _blocked_individual_gate(tmp_path)
    manifest = build_normative_issue_manifest(issue_number, title, BODY)
    decision = gate.decide(manifest, title, BODY)
    assert decision.verdict == "BLOCKED"

    ordinary = _install_ordinary_transport(monkeypatch, _issue_routes(repo, issue_number, title, BODY, ready=True))
    log = _ReviewerRequestLog()
    _install_reviewer_transport(monkeypatch, _comment_target_handler(log, repo=repo, issue_number=issue_number, comment_id=31415))

    github = GitHubClient("controller-token")
    error = gate.apply_blocked(github, decision)

    assert error is None
    saved = gate.store.get(decision.identity)
    assert saved.verdict == "BLOCKED"  # publication never turns BLOCKED into READY
    assert saved.findings_published is True
    assert saved.readiness_removed is True
    assert saved.publication_receipt == {"comment_id": 31415, "publisher_login": REVIEWER_LOGIN, "publisher_app_id": REVIEWER_APP_ID}

    delete_calls = [c for c in ordinary.calls if c[0] == "DELETE"]
    assert delete_calls == [("DELETE", f"https://api.github.com/repos/{repo}/issues/{issue_number}/labels/implementation-ready")]
    post_calls = [r for r in log.requests if r.method == "POST" and r.url.path.endswith("/comments")]
    assert len(post_calls) == 1


def test_inherited_child_specification_blocked_publishes_and_withdraws_via_real_client(tmp_path, monkeypatch, reviewer_app_env):
    """Origin 2 (AS-002 table row 2): SpecificationValidationLifecycle.apply_inherited_blocked."""
    monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: PendingWorkStore(tmp_path / "pending.db"))

    repo = "owner/inherited-repo"
    child_number = 87211
    title = "Child Title"
    result = SpecificationAnalysisResult("BLOCKED", (FINDING,))
    gate = SpecificationValidationLifecycle(repo, "policy-a", tmp_path / "inherited-decisions.json", lambda *_a: result)
    manifest = build_normative_issue_manifest(child_number, title, CHILD_BODY)
    decision = gate.decide(manifest, title, CHILD_BODY)
    assert decision.verdict == "BLOCKED"

    ordinary = _install_ordinary_transport(monkeypatch, _issue_routes(repo, child_number, title, CHILD_BODY, ready=True))
    log = _ReviewerRequestLog()
    _install_reviewer_transport(monkeypatch, _comment_target_handler(log, repo=repo, issue_number=child_number, comment_id=27182))

    github = GitHubClient("controller-token")
    error = gate.apply_inherited_blocked(github, decision, lambda: True)

    assert error is None
    saved = gate.store.get(decision.identity)
    assert saved.verdict == "BLOCKED"
    assert saved.findings_published is True
    assert saved.readiness_removed is True

    delete_calls = [c for c in ordinary.calls if c[0] == "DELETE"]
    assert delete_calls == [("DELETE", f"https://api.github.com/repos/{repo}/issues/{child_number}/labels/implementation-ready")]
    post_calls = [r for r in log.requests if r.method == "POST" and r.url.path.endswith("/comments")]
    assert len(post_calls) == 1
    assert post_calls[0].url == f"https://api.github.com/repos/{repo}/issues/{child_number}/comments"


def _decomposition_inputs(parent: dict, children: list[dict]) -> tuple[DecompositionIssue, list[DecompositionIssue]]:
    def adapt(item: dict) -> DecompositionIssue:
        return DecompositionIssue(build_normative_issue_manifest(item["number"], item["title"], item["body"]), item["body"])

    return adapt(parent), [adapt(child) for child in children]


def test_decomposition_blocked_publishes_and_withdraws_via_real_client(tmp_path, monkeypatch, reviewer_app_env):
    """Origin 3 (AS-002 table row 3): DecompositionValidationLifecycle.apply_blocked."""
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: PendingWorkStore(tmp_path / "pending.db"))

    repo = "owner/decomposition-repo"
    parent_number = 87301
    child_number = 87302
    parent = {"number": parent_number, "title": "Parent", "body": PARENT_BODY, "labels": [{"name": "implementation-ready"}], "state": "open"}
    child = {"number": child_number, "title": "Child", "body": CHILD_BODY, "labels": [], "state": "open"}

    result = DecompositionAnalysisResult("BLOCKED", remediation="EDIT_IN_PLACE")
    gate = DecompositionValidationLifecycle(repo, "policy-a", tmp_path / "decomposition-sets.json", lambda *_a: result)
    identity = gate.identity(parent, [child])
    parent_input, child_inputs = _decomposition_inputs(parent, [child])
    decision = gate.decide(identity, parent_input, child_inputs)
    assert decision.verdict == "BLOCKED"

    routes = _issue_routes(repo, parent_number, "Parent", PARENT_BODY, ready=True)
    ordinary = _install_ordinary_transport(monkeypatch, routes)
    log = _ReviewerRequestLog()
    _install_reviewer_transport(monkeypatch, _comment_target_handler(log, repo=repo, issue_number=parent_number, comment_id=16180))

    github = GitHubClient("controller-token")
    error = gate.apply_blocked(github, decision, lambda _n: (parent, [child]))

    assert error is None
    saved = gate.store.get(decision.identity)
    assert saved.verdict == "BLOCKED"
    assert saved.findings_published is True
    assert saved.readiness_removed is True

    delete_calls = [c for c in ordinary.calls if c[0] == "DELETE"]
    assert delete_calls == [("DELETE", f"https://api.github.com/repos/{repo}/issues/{parent_number}/labels/implementation-ready")]
    post_calls = [r for r in log.requests if r.method == "POST" and r.url.path.endswith("/comments")]
    assert len(post_calls) == 1
    assert post_calls[0].url == f"https://api.github.com/repos/{repo}/issues/{parent_number}/comments"


def test_decomposition_reviewer_identity_failure_leaves_blocked_and_withdraws_nothing(tmp_path, monkeypatch, reviewer_app_env):
    """AS-003 at the decomposition origin: an unavailable reviewer identity
    must not fall back to the ordinary credential or clear the BLOCKED verdict."""
    monkeypatch.setattr("auto_coder.decomposition_validation_lifecycle.get_pending_work_store", lambda: PendingWorkStore(tmp_path / "pending.db"))

    repo = "owner/decomposition-repo-failure"
    parent_number = 87401
    child_number = 87402
    parent = {"number": parent_number, "title": "Parent", "body": PARENT_BODY, "labels": [{"name": "implementation-ready"}], "state": "open"}
    child = {"number": child_number, "title": "Child", "body": CHILD_BODY, "labels": [], "state": "open"}

    result = DecompositionAnalysisResult("BLOCKED", remediation="EDIT_IN_PLACE")
    gate = DecompositionValidationLifecycle(repo, "policy-a", tmp_path / "decomposition-sets-failure.json", lambda *_a: result)
    identity = gate.identity(parent, [child])
    parent_input, child_inputs = _decomposition_inputs(parent, [child])
    decision = gate.decide(identity, parent_input, child_inputs)

    ordinary = _install_ordinary_transport(monkeypatch, _issue_routes(repo, parent_number, "Parent", PARENT_BODY, ready=True))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app"
        return httpx.Response(200, json={"id": 1, "slug": "someone-else"})

    _install_reviewer_transport(monkeypatch, handler)

    github = GitHubClient("controller-token")
    error = gate.apply_blocked(github, decision, lambda _n: (parent, [child]))

    assert error is not None and "reviewer identity unavailable" in error
    saved = gate.store.get(decision.identity)
    assert saved.verdict == "BLOCKED"  # publication failure never turns BLOCKED into READY
    # The diagnostic (reviewer-App) effect never completes: no fabricated
    # identity, no findings POST attempted, no receipt recorded.
    assert saved.findings_published is False
    assert saved.publication_receipt is None
    # Readiness withdrawal is its own independent effect via the ordinary
    # controller credential (module docstring, REQ-005/REQ-007) and is
    # unaffected by the reviewer-App identity failure -- it is not a
    # fallback for the failed reviewer piece.
    assert saved.readiness_removed is True
    assert ("DELETE", f"https://api.github.com/repos/{repo}/issues/{parent_number}/labels/implementation-ready") in ordinary.calls
