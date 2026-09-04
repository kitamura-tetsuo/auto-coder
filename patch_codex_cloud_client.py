import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Find the block where effective_repo config is processed in start_task
    old_block = """        effective_repo = repo_name or self.repo_name
        if effective_repo:
            config = get_llm_config(repo_name=effective_repo)
            backend_cfg = config.get_backend_config(self.backend_name)
            if backend_cfg:
                env_id = backend_cfg.environment_id or getattr(backend_cfg, "environment", None)
                if env_id:
                    self.environment_id = env_id
                if backend_cfg.model:
                    self.model_name = backend_cfg.model
                if backend_cfg.options:
                    self.options = backend_cfg.options
                if backend_cfg.api_key:
                    self.api_key = backend_cfg.api_key
                if backend_cfg.base_url:
                    self.base_url = backend_cfg.base_url
                if backend_cfg.attempts:
                    self.attempts = backend_cfg.attempts

        if not codex_cloud_quota_allows_task():
            raise AutoCoderUsageLimitError("Codex weekly quota is unavailable or below the required threshold")"""

    new_block = """        effective_repo = repo_name or self.repo_name
        strategy = "surplus"
        if effective_repo:
            config = get_llm_config(repo_name=effective_repo)
            strategy = getattr(config, "quota_selection_strategy", None) if config is not None else None
            strategy = strategy if isinstance(strategy, str) else "surplus"
            backend_cfg = config.get_backend_config(self.backend_name)
            if backend_cfg:
                env_id = backend_cfg.environment_id or getattr(backend_cfg, "environment", None)
                if env_id:
                    self.environment_id = env_id
                if backend_cfg.model:
                    self.model_name = backend_cfg.model
                if backend_cfg.options:
                    self.options = backend_cfg.options
                if backend_cfg.api_key:
                    self.api_key = backend_cfg.api_key
                if backend_cfg.base_url:
                    self.base_url = backend_cfg.base_url
                if backend_cfg.attempts:
                    self.attempts = backend_cfg.attempts
        else:
            config = get_llm_config(repo_name=None)
            strategy = getattr(config, "quota_selection_strategy", None) if config is not None else None
            strategy = strategy if isinstance(strategy, str) else "surplus"

        if not codex_cloud_quota_allows_task(strategy=strategy):
            raise AutoCoderUsageLimitError("Codex weekly quota is unavailable or below the required threshold")"""

    content = content.replace(old_block, new_block)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/codex_cloud_client.py')
