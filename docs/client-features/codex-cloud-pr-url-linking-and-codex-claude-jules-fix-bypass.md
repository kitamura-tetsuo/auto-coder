# Codex Cloud PR URL Linking and Codex/Claude Jules Fix Bypass

  codex_claude_pr_handling:
    description: "Append Codex Cloud task URL to PR body when linked issue was processed by Codex Cloud, and prevent Jules fix requests for Codex/Claude PRs."
    implementation: |
      _is_codex_pr, _is_claude_pr, _is_codex_or_claude_pr, _find_codex_cloud_task_for_issue,
      _link_codex_cloud_pr_to_issue, _is_jules_pr, _should_skip_waiting_for_jules,
      _send_jules_error_feedback, process_pull_request in src/auto_coder/pr_processor.py
    behavior:
      - "When a PR description contains `Closes #xxx` (or `Fixes #xxx` / linking keywords):"
        - "If issue `#xxx` was processed using Codex Cloud (tracked via CloudManager or issue comments), appends the Codex Cloud URL (`https://chatgpt.com/codex/tasks/<task_id>`) to the PR body."
        - "Avoids duplicate URL appending if already present."
      - "For PRs created by Codex or Claude (identified by session URLs in the PR body, such as `chatgpt.com/codex/tasks/...` or `claude.ai/code/...`):"
        - "Never sends fix requests or error feedback to Jules (`_send_jules_error_feedback` is bypassed)."
        - "Never converts the PR to Jules mode (`_process_pr_jules_mode` is bypassed)."
        - "Does not wait for Jules on CI failure (`_should_skip_waiting_for_jules` returns False)."
        - "Proceeds directly to branch checkout and local/backend automated fixing."
