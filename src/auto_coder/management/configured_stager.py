"""One controller-owned management stager configuration for all consumers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient

from .self_update_preflight import ManagementPreflightError
from .version_staging import ManagementRuntimeConfig, ManagementVersionStager

if TYPE_CHECKING:
    from .bootstrap_supervisor import BootstrapConfig
    from .oauth_config import ManagementSettings


def configured_management_stager(settings: ManagementSettings, config: BootstrapConfig, *, source_authority: TestedSourcePublisher | None = None) -> ManagementVersionStager:
    if settings.self_update_source_repository is None or settings.self_update_python_executable is None or settings.self_update_uv_executable is None or not settings.self_update_uv_version or config.management_version_root is None:
        raise ManagementPreflightError("staging_unconfigured")
    authority = source_authority or TestedSourcePublisher(GitHubGitDataClient(os.environ.get("GITHUB_TOKEN", ""), MANAGEMENT_REPOSITORY), MANAGEMENT_REPOSITORY)
    return ManagementVersionStager(
        ManagementRuntimeConfig(
            config.instance_id,
            MANAGEMENT_REPOSITORY,
            settings.self_update_source_repository,
            config.management_version_root,
            settings.self_update_python_executable,
            settings.self_update_uv_executable,
            settings.self_update_uv_version,
            protected_roots=(config.state_root, config.oauth_root, config.journal_root),
            management_config=config.management_config,
            source_authority=authority,
        )
    )
