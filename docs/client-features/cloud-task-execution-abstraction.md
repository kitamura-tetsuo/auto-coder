# Cloud Task Execution Abstraction

  cloud_task_abstraction:
    description: "Unified abstraction for asynchronous cloud coding tasks across Jules, Claude Routine, and Codex Cloud."
    implementation: |
      CloudTaskClientBase, CloudTaskState, CloudTask in src/auto_coder/cloud_task_client_base.py,
      JulesClient in src/auto_coder/jules_client.py,
      ClaudeRoutineClient in src/auto_coder/claude_routine_client.py,
      CodexCloudClient in src/auto_coder/codex_cloud_client.py,
      CloudTaskEngine in src/auto_coder/cloud_task_engine.py
    architecture:
      - "CloudTaskClientBase extends LLMClientBase and defines common lifecycle methods: continue_if_paused(), start_task(), get_task(), list_tasks(), stop_task()."
      - "CloudTaskState normalizes task states into: QUEUED, RUNNING, PAUSED, COMPLETED, FAILED, UNKNOWN."
      - "JulesClient implements CloudTaskClientBase, encapsulating Jules-specific state inspection, plan approval, and continuation messaging."
      - "ClaudeRoutineClient implements CloudTaskClientBase. Treats absence of a created PR as substitute paused state, and sends 'continue' messages via 'claude -p --cloud <session_id>' every 1 hour up to 5 hours from start."
      - "CodexCloudClient manages asynchronous Codex Cloud task execution via 'codex cloud exec --env <ENV_ID> [--branch <BRANCH>] PROMPT', 'codex cloud list --json', 'codex cloud status <TASK_ID>', 'codex cloud diff <TASK_ID>', and 'codex cloud apply <TASK_ID>'. continue_if_paused returns False as follow-up messaging is currently unsupported in Codex Cloud CLI."
      - "ClaudeClient and CodexClient remain focused on local/non-cloud execution only."
      - "The orchestration layer interacts with cloud providers uniformly through CloudTaskClientBase without embedding provider-specific state names."
      - "Cloud implementation associations in cloud.csv persist both provider and task/session ID. Adversarial validation resolves this durable ownership and uses CloudTaskClientBase.send_followup; legacy providerless rows, ambiguous ownership, unavailable providers, and providers without follow-up support fail closed without probing another provider. Delivery receipts include provider plus task identity, and follow-up assignment remains independent of paused-task recovery."
