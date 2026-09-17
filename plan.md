1. **Create `src/auto_coder/managed_prompts.py`**
   - Provide `save_managed_prompt`, `get_managed_prompt`, `recover_original_task`.
   - `recover_original_task` will check `===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS` to detect missing metadata and fail properly.

2. **Update Clients for New Tasks**
   - `jules_client.py: start_session`: Add `prepare_cloud_task` (operation="new_task", no_edit=is_noedit) before API call, then `save_managed_prompt` with the returned session_id.
   - `claude_routine_client.py: fire_routine`: Accept `is_noedit` flag (default False). Add `prepare_cloud_task` and `save_managed_prompt`. Update `start_session` and `_run_llm_cli` to pass `is_noedit` through.
   - `codex_cloud_client.py: submit_task`: Accept `is_noedit` flag (default False). Add `prepare_cloud_task`. Because `submit_task` calls `CommandExecutor.run_command`, we get the `task_id` from the output if `result.outcome == ACCEPTED`. Save managed prompt if `task_id` is available. (Actually, also if `outcome == UNCERTAIN`? The issue REQ-006 says "neither composition failure... nor missing prompt metadata may itself authorize a retry, fallback..."). Wait, if `UNCERTAIN` happens, Codex Cloud still assigns a `task_id` from stdout if available, or returns no task_id. If `task_id` is parsed, save it!
   - `codex_cloud_client.py: start_task`: Pass `is_noedit` to `submit_task`.
   - Ensure nested wrappers don't inject twice (done natively because we only inject at the lowest level that performs the send: `start_session`/`fire_routine`/`submit_task`).

3. **Update Follow-Ups (Continuations)**
   - `jules_client.py: send_followup`: Add `prepare_cloud_task` (operation="continuation").
   - `codex_cloud_client.py: send_followup`: Add `prepare_cloud_task` (operation="continuation").
   - Does `claude_routine_client.py` have a `send_followup`? Check it.

4. **Update `jules_engine.py` for Recovery/Continuations**
   - In the session restart logic (`jules_engine.py` around line 253), recover the original task using `recover_original_task` (which uses `managed_prompts.get_managed_prompt`).
   - Use the recovered `original_task` (and original `no_edit` status) to start the replacement session.
   - In the recurrent task check (`jules_engine.py` around line 610), use `recover_original_task` before comparing the session prompt with the local markdown file prompt.

5. **Execute Pre-commit instructions**
   - Run tests and linting.

6. **Submit**
