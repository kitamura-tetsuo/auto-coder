import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Find evaluate_backend_quota and replace the codex evaluations
    old_block_1 = """        usage = get_codex_weekly_usage(now=current_time)
        if usage is None:
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=True,
                usage_retrieval_failed=True,
                reason="Codex Cloud credentials or usage data unavailable",
            )
        if not usage.can_start_task:
            credit_count = usage.reset_credits.available_count
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=False,
                actual_remaining_ratio=usage.remaining_percent / 100.0,
                reset_at=usage.reset_at,
                reset_credit_count=credit_count,
                reset_credit_available=None if credit_count is None else credit_count > 0,
                reason=f"Codex Cloud quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)",
            )"""

    new_block_1 = """        usage = get_codex_weekly_usage(now=current_time)
        if usage is None:
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=True,
                usage_retrieval_failed=True,
                reason="Codex Cloud credentials or usage data unavailable",
            )

        configured_strategy = getattr(config, "quota_selection_strategy", None) if config is not None else None
        strategy = configured_strategy if isinstance(configured_strategy, str) else "surplus"

        can_start = usage.has_remaining_quota if strategy == "burst" else usage.meets_reserve_threshold
        if not can_start:
            credit_count = usage.reset_credits.available_count
            reason_str = f"Codex Cloud quota exhausted ({usage.remaining_percent:.1f}%)" if strategy == "burst" else f"Codex Cloud quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)"
            return BackendQuotaEvaluation(
                backend_name=backend_name,
                is_eligible=False,
                actual_remaining_ratio=usage.remaining_percent / 100.0,
                reset_at=usage.reset_at,
                reset_credit_count=credit_count,
                reset_credit_available=None if credit_count is None else credit_count > 0,
                reason=reason_str,
            )"""

    content = content.replace(old_block_1, new_block_1)

    old_block_2 = """        creds = load_codex_oauth_credentials(now=current_time)
        if creds is not None:
            usage = get_codex_weekly_usage(now=current_time)
            if usage is not None:
                if not usage.can_start_task:
                    credit_count = usage.reset_credits.available_count
                    return BackendQuotaEvaluation(
                        backend_name=backend_name,
                        is_eligible=False,
                        actual_remaining_ratio=usage.remaining_percent / 100.0,
                        reset_at=usage.reset_at,
                        reset_credit_count=credit_count,
                        reset_credit_available=None if credit_count is None else credit_count > 0,
                        reason=f"Codex quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)",
                    )"""

    new_block_2 = """        creds = load_codex_oauth_credentials(now=current_time)
        if creds is not None:
            usage = get_codex_weekly_usage(now=current_time)
            if usage is not None:
                configured_strategy = getattr(config, "quota_selection_strategy", None) if config is not None else None
                strategy = configured_strategy if isinstance(configured_strategy, str) else "surplus"

                can_start = usage.has_remaining_quota if strategy == "burst" else usage.meets_reserve_threshold
                if not can_start:
                    credit_count = usage.reset_credits.available_count
                    reason_str = f"Codex quota exhausted ({usage.remaining_percent:.1f}%)" if strategy == "burst" else f"Codex quota insufficient ({usage.remaining_percent:.1f}% < {usage.minimum_remaining_percent:.1f}%)"
                    return BackendQuotaEvaluation(
                        backend_name=backend_name,
                        is_eligible=False,
                        actual_remaining_ratio=usage.remaining_percent / 100.0,
                        reset_at=usage.reset_at,
                        reset_credit_count=credit_count,
                        reset_credit_available=None if credit_count is None else credit_count > 0,
                        reason=reason_str,
                    )"""

    content = content.replace(old_block_2, new_block_2)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/quota_selector.py')
