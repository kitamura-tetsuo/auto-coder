"""Controlled authoritative CI observations for PR lifecycle tests."""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Callable

from auto_coder.ci_observation import CIConclusion
from auto_coder.util.github_action import GitHubActionsStatusResult


class _Checks:
    def __init__(self, status: str, conclusion: str | None, identifier: int | None, unavailable: bool) -> None:
        self._run = {
            "id": identifier,
            "name": "PR Tests",
            "status": status,
            "conclusion": conclusion,
            "details_url": f"https://github.test/actions/runs/{identifier}",
        }
        self._unavailable = unavailable

    def list_for_ref(self, _owner: str, _repo: str, *, ref: str, **_kwargs: object) -> dict[str, object]:
        if self._unavailable:
            raise ValueError("controlled CI observation unavailable")
        if self._run["id"] is None:
            return {"check_runs": []}
        return {"check_runs": [{**self._run, "head_sha": ref, "app": {"id": 1}}]}


class _Actions:
    def list_workflow_runs_for_repo(self, _owner: str, _repo: str, **_kwargs: object) -> dict[str, object]:
        return {"workflow_runs": []}


def authoritative_ci_status(
    *,
    success: bool = True,
    ids: list[int] | None = None,
    in_progress: bool = False,
    error: str | None = None,
    waiting_runs: list[tuple[int, int, str]] | None = None,
    namespace: str | None = None,
) -> Callable[..., GitHubActionsStatusResult]:
    """Build a side effect whose snapshot participates in the real phase cache."""
    configured_ids = list(ids or [])

    def read(repository: str, pr_data: dict[str, object], *_args: object, **_kwargs: object) -> GitHubActionsStatusResult:
        pr_number = int(pr_data["number"])
        head = str((pr_data.get("head") or {}).get("sha") or "controlled-head")  # type: ignore[union-attr]
        identifier = configured_ids[0] if configured_ids else None
        effective_success = success and error is None
        conclusion = CIConclusion.PENDING if in_progress else (CIConclusion.SUCCESS if effective_success else CIConclusion.FAILURE)
        status = "in_progress" if conclusion is CIConclusion.PENDING else "completed"
        api = SimpleNamespace(
            checks=_Checks(status, None if in_progress else conclusion.value, identifier, error is not None),
            actions=_Actions(),
        )
        module_namespace = namespace or ("src.auto_coder" if any(type(arg).__module__.startswith("src.auto_coder") for arg in _args) else "auto_coder")
        observer = import_module(f"{module_namespace}.github_ci_observer").observe_ci
        result_type = import_module(f"{module_namespace}.util.github_action").GitHubActionsStatusResult
        snapshot = observer(api, "controlled-token", repository, pr_number, head, api_origin="https://api.github.test")
        return result_type(
            success=effective_success,
            ids=configured_ids,
            in_progress=in_progress,
            error=error,
            waiting_runs=list(waiting_runs or []),
            observation=snapshot,
        )

    return read
