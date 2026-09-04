import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Move the test inside the TestCodexCloudClient class
    test_block = """def test_start_task_burst_strategy_bypasses_reserve_execution_gate(mock_backend_config):
    \"\"\"Test REQ-006: Execution gate respects burst strategy and allows start when below reserve.\"\"\"
    mock_backend_config.quota_selection_strategy = "burst"
    with (
        patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config),
        patch("auto_coder.codex_usage_checker.get_codex_weekly_usage") as mock_get_usage,
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as mock_run,
    ):
        from auto_coder.codex_usage_checker import CodexWeeklyUsage
        from datetime import datetime, timezone, timedelta

        # 12% usage, 15% reserve. Fails surplus, passes burst.
        mock_get_usage.return_value = CodexWeeklyUsage(
            remaining_percent=12.0,
            reset_at=datetime.now(timezone.utc) + timedelta(days=2),
            days_until_reset=2,
            minimum_remaining_percent=15.0,
        )

        # Mock run_command to succeed so it doesn't raise a separate error
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"task_id": "task_e_123"}'

        client = CodexCloudClient("codex-cloud")
        task_id = client.start_task("Implement the issue")

        assert task_id == "task_e_123"
        mock_run.assert_called_once()"""

    new_test_block = """    def test_start_task_burst_strategy_bypasses_reserve_execution_gate(self, mock_backend_config):
        \"\"\"Test REQ-006: Execution gate respects burst strategy and allows start when below reserve.\"\"\"
        mock_backend_config.quota_selection_strategy = "burst"
        with (
            patch("auto_coder.codex_cloud_client.get_llm_config", return_value=mock_backend_config),
            patch("auto_coder.codex_usage_checker.get_codex_weekly_usage") as mock_get_usage,
            patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as mock_run,
        ):
            from auto_coder.codex_usage_checker import CodexWeeklyUsage
            from datetime import datetime, timezone, timedelta

            # 12% usage, 15% reserve. Fails surplus, passes burst.
            mock_get_usage.return_value = CodexWeeklyUsage(
                remaining_percent=12.0,
                reset_at=datetime.now(timezone.utc) + timedelta(days=2),
                days_until_reset=2,
                minimum_remaining_percent=15.0,
            )

            # Mock run_command to succeed so it doesn't raise a separate error
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '{"task_id": "task_e_123"}'

            client = CodexCloudClient("codex-cloud")
            task_id = client.start_task("Implement the issue")

            assert task_id == "task_e_123"
            mock_run.assert_called_once()"""

    content = content.replace(test_block, "")
    content = content.replace("    def test_list_tasks(self, mock_backend_config):", new_test_block + "\n\n    def test_list_tasks(self, mock_backend_config):")

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('tests/test_codex_cloud_client.py')
